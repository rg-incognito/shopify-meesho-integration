"""Telegram bot: share Meesho links -> AI listing preview -> Approve -> pushed to Shopify.

Also runs the scheduled jobs (push approved rows, Meesho price/stock re-check), so this is the
only process that needs to stay running on the PC.
"""
import asyncio
import html
import json
import logging
import random
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from telegram import (ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument, InputMediaPhoto,
                      Update)
from telegram.constants import ParseMode
from telegram.error import NetworkError
from telegram.request import HTTPXRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from pipeline import colour_posts, gemini_web, instagram, prompt_library, kaggle_posts, manual_posts, music, publisher, reels, trending, viral
from pipeline.config import CFG, env, setup_logging
from pipeline.posts import PostSheet, post_dir, post_images, reference_image, styles
from pipeline.workflow import Pipeline

setup_logging("bot")
log = logging.getLogger("bot")

URL_RE = re.compile(r"https?://[^\s]*(?:meesho\.com|msho\.in)[^\s]*", re.I)
ALLOWED = {int(x) for x in env("TELEGRAM_ALLOWED_USER_IDS", required=False).replace(" ", "").split(",") if x}
IST = ZoneInfo("Asia/Kolkata")

# Every pipeline call runs on this one thread (Playwright's sync API is single-threaded).
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")
# Reel rendering is CPU-heavy but needs no browser/sheet: its own thread so the bot stays responsive.
_render_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render")
# Gemini app in Chrome (long runs, its own browser): own thread, one run at a time.
_gemini_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gemini")
_gemini_busy = False
_pipeline: Pipeline | None = None


def _get_pipeline() -> Pipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline()
    return _pipeline


class RetryingRequest(HTTPXRequest):
    """Retries outgoing Bot API calls on network errors, so a flaky connection
    doesn't silently drop replies (a retried call may occasionally send twice)."""

    async def do_request(self, *args, **kwargs):
        for attempt in range(6):
            try:
                return await super().do_request(*args, **kwargs)
            except NetworkError as e:
                if attempt == 5:
                    raise
                log.warning("Telegram send failed (%s), retry %s/5", e, attempt + 1)
                await asyncio.sleep(2 * (attempt + 1))


async def run(method, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, lambda: method(_get_pipeline(), *args))


def esc(s) -> str:
    return html.escape(str(s))


async def authorized(update: Update) -> bool:
    user = update.effective_user
    if user and user.id in ALLOWED:
        return True
    log.warning("Unauthorised user %s (@%s) tried the bot", user.id if user else "?", user.username if user else "?")
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Not authorised. Your Telegram user ID is {user.id if user else '?'}.\n"
            "Add it to TELEGRAM_ALLOWED_USER_IDS in .env and restart the bot.")
    return False


_posts: PostSheet | None = None


_reels_sheet = None


def reels_sheet(p: Pipeline):
    global _reels_sheet
    if _reels_sheet is None:
        _reels_sheet = reels.ReelSheet(p.sheet.ws.spreadsheet)
    return _reels_sheet


def posts_sheet(p: Pipeline) -> PostSheet:
    global _posts
    if _posts is None:
        _posts = PostSheet(p.sheet.ws.spreadsheet)
    return _posts


def post_buttons(mid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve post", callback_data=f"pa:{mid}"),
                                  InlineKeyboardButton("🔄 Regenerate", callback_data=f"pr:{mid}"),
                                  InlineKeyboardButton("⏭ Skip", callback_data=f"ps:{mid}")]])


def review_buttons(mid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"ap:{mid}"),
         InlineKeyboardButton("🔄 New images", callback_data=f"rg:{mid}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"rj:{mid}")],
        [InlineKeyboardButton("📁 Collections", callback_data=f"cl:{mid}")],
    ])


def live_buttons(mid: str, status: str) -> InlineKeyboardMarkup:
    toggle = (InlineKeyboardButton("⏸ Back to Draft", callback_data=f"dr:{mid}") if status == "ACTIVE"
              else InlineKeyboardButton("🟢 Make Active", callback_data=f"ac:{mid}"))
    return InlineKeyboardMarkup([[toggle, InlineKeyboardButton("📁 Collections", callback_data=f"cl:{mid}")]])


def collection_buttons(mid: str, cols: list[dict], selected: set[str]) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(("✓ " if c["id"] in selected else "") + c["title"][:30],
                                    callback_data=f"ct:{mid}:{c['id'].rsplit('/', 1)[-1]}") for c in cols]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("➕ New collection", callback_data=f"cn:{mid}"),
                 InlineKeyboardButton("✔ Done", callback_data=f"cd:{mid}")])
    return InlineKeyboardMarkup(rows)


async def show_collections(chat, mid: str, message=None):
    cols, selected = await run(Pipeline.collection_choices, mid)
    text = (f"📁 Collections for <code>{esc(mid)}</code>: tap to add/remove (✓ = in collection)."
            + ("" if cols else "\nNo manual collections yet; create one."))
    markup = collection_buttons(mid, cols, selected)
    if message:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def send_preview(chat, s: dict):
    if s["images"]:
        media = []
        for p in s["images"][:10]:
            with open(p, "rb") as f:
                media.append(InputMediaPhoto(f.read()))
        await chat.send_media_group(media)
    compare = f" <s>₹{s['compare_at']}</s>" if s["compare_at"] else ""
    text = (
        f"<b>{esc(s['title'])}</b>\n"
        f"ID: <code>{esc(s['meesho_id'])}</code>\n"
        f"Meesho cost: ₹{esc(s['meesho_price'])}\n"
        f"Your price: <b>₹{esc(s['my_price'])}</b>{compare}\n"
        f"Sizes: {esc(', '.join(s['sizes']))}\n"
        f"In stock: {'yes' if s['in_stock'] else 'NO'}\n"
        f"<a href=\"{esc(s['url'])}\">Meesho page</a>\n\n"
        f"Change price: <code>/price {esc(s['meesho_id'])} 699</code>"
    )
    if s.get("warning") and not s.get("ai_images"):
        text += f"\n\n⚠️ {esc(s['warning'])}"
    await chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=review_buttons(s["meesho_id"]),
                            disable_web_page_preview=True)


async def send_post_preview(chat, mid: str):
    row = next((r for r in await run(lambda p: posts_sheet(p).all()) if str(r["meesho_id"]) == mid), None)
    imgs = post_images(mid)
    if not row or not imgs:
        await chat.send_message(f"No generated post for {mid}.")
        return
    media = []
    for f in imgs:
        with open(f, "rb") as fh:
            media.append(InputMediaPhoto(fh.read()))
    await chat.send_media_group(media)
    await chat.send_message(f"📸 Post for <code>{esc(mid)}</code> ({esc(row['audience'])})\n\n{esc(row['caption'])}",
                            parse_mode=ParseMode.HTML, reply_markup=post_buttons(mid),
                            disable_web_page_preview=True)


async def push_and_report(chat, mid: str):
    msg = await chat.send_message(f"⬆️ Pushing {mid} to Shopify…")
    try:
        r = await run(Pipeline.push, mid)
        verb = "Created" if r["created"] else "Updated"
        cols = f"\nCollections: {', '.join(r['collections'])}" if r.get("collections") else ""
        await msg.edit_text(f"✅ {verb} on Shopify as {r['status']}: {r['admin_url']}{cols}",
                            reply_markup=live_buttons(mid, r["status"]), disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"❌ Push failed for {mid}: {esc(e)}")


# --- hand-made carousels (/originals -> Gemini app -> images back) ------------------------------
def carousel_buttons(mid: str, real_first: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Post now", callback_data=f"mp:{mid}"),
         InlineKeyboardButton("🕒 Add to queue", callback_data=f"mq:{mid}")],
        [InlineKeyboardButton(("✓ " if real_first else "") + "🖼 Real photo first", callback_data=f"mr:{mid}"),
         InlineKeyboardButton("✏️ Edit caption", callback_data=f"me:{mid}")],
        [InlineKeyboardButton("🗑 Start over", callback_data=f"mc:{mid}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"mx:{mid}")],
    ])


def _find_products(p: Pipeline, query: str) -> list[dict]:
    """Exact product ID, else every product whose title contains all the words (live ones first)."""
    q = query.lower().strip()
    rows = p.sheet.all()
    exact = [r for r in rows if str(r["meesho_id"]).lower() == q]
    if exact:
        return exact
    words = q.split()
    hits = [r for r in rows if words and all(w in f"{r['shopify_title']} {r['meesho_title']}".lower() for w in words)]
    return sorted(hits, key=lambda r: r["status"] != "PUSHED")


def _post_row(p: Pipeline, mid: str) -> dict:
    return next((r for r in posts_sheet(p).all() if str(r["meesho_id"]) == mid), {})


def _open_carousel(p: Pipeline, mid: str) -> tuple[list, dict]:
    row = _post_row(p, mid)
    status = str(row.get("post_status", ""))
    if status == "GENERATING":
        raise RuntimeError("the Kaggle batch is making images for this product right now; try after it finishes")
    if row.get("source") == "manual" and status in ("GENERATED", "APPROVED", "ERROR"):
        status = manual_posts.opts(mid).get("prev_status") or "QUEUED"   # reopening an unposted draft
    manual_posts.open_draft(mid, status)
    return Pipeline.clean_images(mid), p.sheet.get(mid) or {}


def _build_carousel(p: Pipeline, mid: str) -> tuple[list, dict]:
    if not manual_posts.uploads(mid):
        return [], {}
    real = Pipeline.clean_images(mid)[:1] if manual_posts.opts(mid).get("real_first") else []
    files = manual_posts.build(mid, real[0] if real else None)
    return files, manual_posts.save_post(mid, p.sheet.get(mid) or {}, posts_sheet(p), files)


def _cancel_carousel(p: Pipeline, mid: str) -> None:
    prev = manual_posts.opts(mid).get("prev_status") or "QUEUED"
    if prev in ("GENERATED", "APPROVED", "GENERATING"):   # its Kaggle images were replaced: make new ones
        prev = "QUEUED"
    manual_posts.clear(mid)
    manual_posts.close_draft(mid)
    if _post_row(p, mid):
        posts_sheet(p).upsert(mid, post_status=prev, source="", caption="", images="", error="")


async def send_originals(chat, mid: str):
    try:
        files, prow = await run(_open_carousel, mid)
    except Exception as e:
        await chat.send_message(f"❌ {mid}: {e}")
        return
    ids = []
    if files:
        media = []
        for i, f in enumerate(files[:10], 1):
            with open(f, "rb") as fh:
                media.append(InputMediaDocument(fh.read(), filename=f"{mid}-{i}.jpg"))
        ids += [m.message_id for m in await chat.send_media_group(media)]
    msg = await chat.send_message(
        f"🎨 <b>{esc(prow.get('shopify_title') or mid)}</b> (<code>{esc(mid)}</code>)\n"
        f"{'Original photos above (full quality).' if files else '⚠️ No original photos saved for this product.'}\n\n"
        "1. In the Gemini app, attach an original photo and paste one of the prompts below "
        "(tap a prompt to copy it; one prompt = one slide).\n"
        "2. <b>Reply to this message</b> with the images (up to 10, as an album or one by one, in the order "
        "you want them in the carousel).\n"
        "I'll show you the carousel before anything is posted.",
        parse_mode=ParseMode.HTML)
    manual_posts.remember_messages(mid, ids + [msg.message_id])
    await send_prompts(chat, mid)


async def send_prompts(chat, mid: str):
    try:
        shots = await run(lambda p: manual_posts.prompts(mid, p.sheet.get(mid) or {}, posts_sheet(p).recent_scenes()))
    except Exception as e:
        log.exception("prompts %s failed", mid)
        await chat.send_message(f"⚠️ Could not make prompts for {mid}: {e}")
        return
    ids = []
    for i, s in enumerate(shots, 1):
        last = i == len(shots)
        msg = await chat.send_message(
            f"<b>Slide {i} · {esc(s['name'])}</b> ({esc(s['scene'])})\n<pre>{esc(s['prompt'])}</pre>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 New prompts", callback_data=f"mn:{mid}")]])
            if last else None)
        ids.append(msg.message_id)
    msg = await chat.send_message(
        "Or let me make a single-image post: I pick a style from prompts/, open Gemini in a Chrome window on "
        f"the PC and make 1 image (max {gemini_web.PER_DAY} a day).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Make 1 image in Gemini", callback_data=f"gw:{mid}")]]))
    ids.append(msg.message_id)
    manual_posts.remember_messages(mid, ids)


async def send_carousel_preview(chat, mid: str):
    files, info = await run(_build_carousel, mid)
    if not files:
        msg = await chat.send_message(f"No images for {mid} yet: reply to this message with the AI images.")
        manual_posts.remember_messages(mid, [msg.message_id])
        return
    media = []
    for f in files:
        with open(f, "rb") as fh:
            media.append(InputMediaPhoto(fh.read()))
    album = await chat.send_media_group(media)
    o = manual_posts.opts(mid)
    n_up = len(manual_posts.uploads(mid))
    extra = (f"\n⚠️ {n_up + bool(o.get('real_first'))} images; Instagram allows 10, so only the first 10 are used."
             if n_up + bool(o.get("real_first")) > manual_posts.MAX_SLIDES else "")
    warn = f"\n\n⚠️ {esc(info['warning'])}" if info.get("warning") else ""
    msg = await chat.send_message(
        f"🎠 Carousel for <code>{esc(mid)}</code>: {len(files)} slides{extra}\n"
        "Reply with more images to add them; 🗑 Start over to replace them.\n\n"
        f"{esc(info['caption'])}{warn}",
        parse_mode=ParseMode.HTML, reply_markup=carousel_buttons(mid, bool(o.get("real_first"))),
        disable_web_page_preview=True)
    manual_posts.remember_messages(mid, [m.message_id for m in album] + [msg.message_id])


def _gemini_cfg() -> dict:
    return styles().get("gemini_web") or {}


async def gemini_make(chat, mid: str) -> bool:
    """One single-image post: a style from prompts/ (women/men/kid), filled in for this product, made in the
    Gemini app (Chrome on this PC), then shown for approval. True if an image was made."""
    global _gemini_busy
    if _gemini_busy:
        await chat.send_message("🤖 Gemini is already making an image; try when it's done.")
        return False
    n, why = gemini_web.allowance()
    if not n:
        await chat.send_message(f"⏸ Gemini (Chrome) is off: {why}. You can still make images by hand with /originals.")
        return False
    _gemini_busy = True
    loop = asyncio.get_running_loop()
    try:
        files, prow = await run(_open_carousel, mid)
        if not files:
            await chat.send_message(f"⚠️ No original photos saved for {mid}, so there's nothing to give Gemini.")
            return False
        work = gemini_web.DATA_DIR / "gemini_web" / mid
        ref = reference_image(mid, work)
        spec = await loop.run_in_executor(
            _gemini_executor, lambda: prompt_library.make(mid, prow, ref, _gemini_cfg().get("faces", "hidden")))
        if not spec:
            await chat.send_message(f"⚠️ {mid}: no prompt set fits this product (only women / men / kid wear).")
            return False
        plan_file = post_dir(mid) / "plan.json"     # recorded like other posts (scene variety)
        plan_file.write_text(json.dumps([{"name": spec["style"], "scene": f"{spec['folder']}/{spec['style']}",
                                          "prompt": spec["prompt"]}], indent=1, ensure_ascii=False), encoding="utf-8")
        note = await chat.send_message(
            f"🤖 Making 1 image for <code>{esc(mid)}</code> in Gemini: <i>{esc(spec['folder'])}/{esc(spec['style'])}</i> "
            "(a Chrome window opens on the PC; don't use it meanwhile). About 2 min.", parse_mode=ParseMode.HTML)
        res = await loop.run_in_executor(_gemini_executor, lambda: gemini_web.generate(ref, [spec["prompt"]], work))
        try:
            await note.delete()
        except Exception:
            pass
        if res["stopped"]:
            await chat.send_message(f"⏸ Gemini stopped: {esc(res['stopped'])}. No more Gemini runs today; "
                                    "check the Chrome window / your Gemini account.", parse_mode=ParseMode.HTML)
        if not res["files"]:
            if not res["stopped"]:
                await chat.send_message(f"⚠️ Gemini came back without an image for {mid}; it stays in the queue.")
            await run(_cancel_carousel, mid)
            return False
        prompt_library.mark_used(spec["folder"], spec["template"], mid)
        await run(lambda p: manual_posts.clear(mid))
        await run(lambda p, d=res["files"][0].read_bytes(): manual_posts.add_upload(mid, d))
        await send_carousel_preview(chat, mid)
        return True
    except Exception as e:
        log.exception("gemini %s failed", mid)
        await chat.send_message(f"❌ Gemini run for {mid} failed: {e}")
        return False
    finally:
        _gemini_busy = False


def _gemini_pick(p: Pipeline, n: int) -> list[str]:
    """Next QUEUED posts whose product is live on the store, has its original photos and fits a prompt
    set (the nightly Kaggle batch has already taken the first ones of the queue)."""
    from post import queue as queue_posts
    ps = posts_sheet(p)
    queue_posts(p.sheet, ps)
    out = []
    for r in ps.next("QUEUED", 80):
        mid = str(r["meesho_id"])
        prow = p.sheet.get(mid) or {}
        if prow.get("status") == "PUSHED" and str(prow.get("shopify_status", "")).upper() == "ACTIVE" \
                and Pipeline.clean_images(mid) and prompt_library.folder_for(mid, prow):
            out.append(mid)
            if len(out) >= n:
                break
    return out


async def job_gemini_auto(ctx: ContextTypes.DEFAULT_TYPE):
    """Scheduled: single-image posts for the next queued products, each sent for approval."""
    if _gemini_busy or not ALLOWED:
        return
    n, why = gemini_web.allowance()
    if not n:
        log.info("gemini auto skipped: %s", why)
        return
    mids = await run(_gemini_pick, min(n, int(_gemini_cfg().get("products_per_run", 2))))
    if not mids:
        log.info("gemini auto: no QUEUED product to make")
        return
    chat = await ctx.bot.get_chat(next(iter(ALLOWED)))
    for i, mid in enumerate(mids):
        if i:
            await asyncio.sleep(random.uniform(*gemini_web.PAUSE))
        if not gemini_web.allowance()[0]:
            break
        await gemini_make(chat, mid)


# --- weekly colour carousel (same design in several colours) ---------------------------------------
def colour_buttons(gid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Post now", callback_data=f"kp:{gid}")],
        [InlineKeyboardButton("🔄 Another design", callback_data=f"kn:{gid}"),
         InlineKeyboardButton("❌ Discard", callback_data=f"kx:{gid}")],
    ])


_colour_busy = False


async def make_colour_post(chat, exclude: set[str] = frozenset()):
    """Find the next same-design-several-colours group, build the carousel and send it for approval."""
    global _colour_busy
    if _colour_busy:
        await chat.send_message("🎨 Already looking for a colour carousel; it'll arrive shortly.")
        return
    _colour_busy = True
    note = await chat.send_message("🎨 Looking for a design that comes in several colours (Gemini checks the photos)…")
    loop = asyncio.get_running_loop()
    try:
        group = await run(lambda p: colour_posts.find_group(p.sheet.all(), exclude=set(exclude)))
        if not group:
            await note.edit_text("🎨 No new design with several colours found (all live colour sets were posted in the "
                                 f"last {colour_posts.REPOST_DAYS} days, skipped, or didn't match by eye).")
            return
        files = await loop.run_in_executor(_render_executor, lambda: colour_posts.build(group))
        text = await run(lambda p: colour_posts.caption(group, p.sheet))
        st = colour_posts.load_state()
        st["groups"][group["id"]] = {**group, "status": "DRAFT", "caption": text,
                                     "created": datetime.now().isoformat(timespec="seconds")}
        colour_posts.save_state(st)
        media = []
        for f in files:
            with open(f, "rb") as fh:
                media.append(InputMediaPhoto(fh.read()))
        await chat.send_media_group(media)
        await chat.send_message(f"🎨 Weekly colour carousel: {len(group['mids'])} colours "
                                f"({', '.join(esc(m) for m in group['mids'])})\n\n{esc(text)}",
                                parse_mode=ParseMode.HTML, reply_markup=colour_buttons(group["id"]))
        await note.delete()
    except Exception as e:
        log.exception("colour carousel failed")
        await chat.send_message(f"❌ Colour carousel failed: {e}")
    finally:
        _colour_busy = False


async def colour_button(chat, q, action: str, gid: str):
    st = colour_posts.load_state()
    g = st["groups"].get(gid)
    if not g or g.get("status") != "DRAFT":
        await chat.send_message("That colour carousel isn't waiting any more (posted or discarded).")
        return
    if action == "kp":
        await q.edit_message_reply_markup(None)
        note = await chat.send_message("📤 Posting the colour carousel…")
        try:
            res = await run(lambda p: colour_posts.publish(gid, p.sheet))
            if res.get("manual"):
                await note.edit_text("📋 Colour carousel ready to post by hand: files + caption sent below.")
                await send_manual(chat.get_bot(), res["manual"])
                return
            fb = (f"\n✅ Facebook: {res['fb_permalink']}" if res.get("fb_permalink")
                  else f"\n⚠️ Facebook failed: {res['fb_error']}" if res.get("fb_error") else "")
            await note.edit_text(f"✅ Colour carousel posted: {res['permalink']}{fb}\n"
                                 "\"Link\" comments get the link for every colour.")
        except Exception as e:
            log.exception("colour post %s failed", gid)
            await note.edit_text(f"❌ Posting failed: {e}")
        return
    g["status"] = "SKIPPED" if action == "kx" else "PASSED"
    colour_posts.save_state(st)
    await q.edit_message_reply_markup(None)
    if action == "kn":
        await make_colour_post(chat, exclude=set(g["mids"]))
    else:
        await chat.send_message("❌ Discarded; this design won't be suggested again.")


async def job_colour_post(ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED:
        await make_colour_post(await ctx.bot.get_chat(next(iter(ALLOWED))))


async def cmd_colours(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    await make_colour_post(update.effective_chat)


async def cmd_gemini(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    if not ctx.args:
        n, why = gemini_web.allowance()
        await update.message.reply_text(
            "Usage: /gemini <product ID or words from the title>\n"
            "Makes a single-image post in the Gemini app on this PC (a style from prompts/), then shows it.\n"
            "It also runs by itself at " + ", ".join((styles().get("gemini_web") or {}).get("run_at") or ["(off)"])
            + " on the next queued product.\n"
            + (f"Can make {n} image(s) now." if n else f"Off right now: {why}."))
        return
    hits = await run(_find_products, " ".join(ctx.args))
    if not hits:
        await update.message.reply_text("No product matches that. Try the product ID or fewer words.")
    elif len(hits) == 1:
        await gemini_make(update.effective_chat, str(hits[0]["meesho_id"]))
    else:
        rows = [[InlineKeyboardButton(f"{r['shopify_title'] or r['meesho_title']}"[:50] + f" · ₹{r['my_price']}",
                                      callback_data=f"gn:{r['meesho_id']}")] for r in hits[:8]]
        await update.message.reply_text("Which product?", reply_markup=InlineKeyboardMarkup(rows))


_bursts: dict[int, dict] = {}


async def on_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Collect images that arrive together (an album, or several sent in a row), then build the carousel."""
    if not await authorized(update):
        return
    msg, chat_id = update.message, update.effective_chat.id
    b = _bursts.setdefault(chat_id, {"msgs": [], "reply_to": None, "caption": ""})
    b["msgs"].append(msg)
    if msg.reply_to_message:
        b["reply_to"] = msg.reply_to_message.message_id
    if msg.caption:
        b["caption"] += " " + msg.caption
    name = f"burst:{chat_id}"
    for j in ctx.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    ctx.job_queue.run_once(job_image_burst, 4, chat_id=chat_id, name=name)


async def job_image_burst(ctx: ContextTypes.DEFAULT_TYPE):
    b = _bursts.pop(ctx.job.chat_id, None)
    if not b:
        return
    chat = await ctx.bot.get_chat(ctx.job.chat_id)
    known = await run(lambda p: {str(r["meesho_id"]).lower() for r in p.sheet.all()})
    mid = manual_posts.draft_for(b["reply_to"], b["caption"], known)
    if not mid:
        await chat.send_message("Which product are these for? Send /originals <product name or ID> first and "
                                "reply to that message with the images (or put the product ID in the caption).")
        return
    if mid not in manual_posts._load()["opts"]:   # product ID in the caption, no /originals before
        try:
            await run(_open_carousel, mid)
        except Exception as e:
            await chat.send_message(f"❌ {mid}: {e}")
            return
    note = await chat.send_message(f"📥 Got {len(b['msgs'])} image(s) for {mid}, building the carousel…")
    try:
        for m in sorted(b["msgs"], key=lambda m: m.message_id):
            tg_file = await (m.photo[-1] if m.photo else m.document).get_file()
            data = bytes(await tg_file.download_as_bytearray())
            await run(lambda p, d=data: manual_posts.add_upload(mid, d))
        await send_carousel_preview(chat, mid)
    except Exception as e:
        log.exception("carousel %s failed", mid)
        await chat.send_message(f"❌ Could not build the carousel for {mid}: {e}")
    finally:
        try:
            await note.delete()
        except Exception:
            pass


async def carousel_button(chat, q, ctx, action: str, mid: str):
    if action == "og":   # picked a product from /originals search results
        await q.edit_message_reply_markup(None)
        await send_originals(chat, mid)
    elif action == "mn":
        await q.edit_message_reply_markup(None)
        await send_prompts(chat, mid)
    elif action == "mr":
        o = manual_posts.set_opt(mid, real_first=not manual_posts.opts(mid).get("real_first"))
        await q.edit_message_reply_markup(None)
        await chat.send_message("🖼 Real photo added as slide 1." if o["real_first"] else "🖼 Real photo removed.")
        await send_carousel_preview(chat, mid)
    elif action == "me":
        ctx.user_data["caption_for"] = mid
        await chat.send_message(
            f"Send the new caption for {mid}. Write {{link}} where the product link should go "
            "(or leave it out).", reply_markup=ForceReply(input_field_placeholder="Caption"))
    elif action == "mc":
        await q.edit_message_reply_markup(None)
        await run(lambda p: manual_posts.clear(mid))
        msg = await chat.send_message(f"🗑 Cleared the images for {mid}. Reply to this message with the new ones.")
        manual_posts.remember_messages(mid, [msg.message_id])
    elif action == "mx":
        await q.edit_message_reply_markup(None)
        await run(_cancel_carousel, mid)
        await chat.send_message(f"❌ Carousel for {mid} cancelled; nothing was posted.")
    elif action == "mq":
        await run(lambda p: posts_sheet(p).upsert(mid, post_status="APPROVED"))
        await q.edit_message_reply_markup(None)
        manual_posts.close_draft(mid)
        times = ", ".join(styles().get("post_times") or [])
        await chat.send_message(f"🕒 {mid} is in the queue; it goes out at one of the posting times ({times}), "
                                "after the approved posts above it in the Posts tab.")
    elif action == "mp":
        await q.edit_message_reply_markup(None)
        note = await chat.send_message(f"📤 Posting {mid} to Instagram + Facebook…")
        res = await run(lambda p: publisher.publish(_post_row(p, mid), p.sheet, posts_sheet(p)))
        if res.get("no_link"):
            text = f"❌ {mid} isn't Active on the store, so there's no link to post. Use /activate {mid}, then tap Post now again."
            await q.edit_message_reply_markup(carousel_buttons(mid, bool(manual_posts.opts(mid).get("real_first"))))
        elif res.get("no_images"):
            text = f"❌ The carousel images for {mid} are missing; send them again."
        elif res.get("manual"):
            await send_manual(chat.get_bot(), res["manual"])
            manual_posts.close_draft(mid)
            text = f"📋 {mid} is ready to post by hand: files + caption sent below."
        else:
            text = publish_report(res)
            if res.get("posted"):
                manual_posts.close_draft(mid)
        await note.edit_text(text, disable_web_page_preview=True)


# --- manual publishing (publish_mode: manual) ------------------------------------------------------
MANUAL_KINDS = {"p": "Post", "r": "Reel", "c": "Colour carousel"}


async def send_manual(bot, m: dict):
    """Sends a ready post (files + caption to copy) to post by hand in Meta Business Suite."""
    files = [Path(f) for f in m["files"]]
    what = MANUAL_KINDS.get(m["kind"], "Post")
    text = (f"📋 <b>{what} {esc(m['key'])} is ready.</b> Post it to Instagram + Facebook in Meta Business Suite "
            f"(Create post / Create reel, tick both), then tap ✅ Mark posted.\n"
            f"Files are also in <code>{esc(m['folder'])}</code>\n\nCaption (tap to copy):\n"
            f"<pre>{esc(m['caption'])}</pre>")
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Mark posted", callback_data=f"mk:{m['kind']}:{m['key']}")]])
    for uid in ALLOWED:
        try:
            if m["kind"] == "r":
                await bot.send_document(uid, files[0].read_bytes(), filename=files[0].name)
            elif len(files) == 1:
                await bot.send_photo(uid, files[0].read_bytes())
            else:
                await bot.send_media_group(uid, [InputMediaPhoto(f.read_bytes()) for f in files[:10]])
            await bot.send_message(uid, text[:4096], parse_mode=ParseMode.HTML, reply_markup=buttons,
                                   disable_web_page_preview=True)
        except Exception:
            log.exception("Could not send manual post %s to %s", m["key"], uid)


async def mark_posted(kind: str, key: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if kind == "p":
        await run(lambda p: posts_sheet(p).upsert(key, post_status="POSTED", posted_at=now, error=""))
    elif kind == "r":
        state = reels.load_state()
        if key in state:
            state[key]["status"] = "POSTED"
            reels.save_state(state)
        await run(lambda p: reels_sheet(p).upsert(key, status="POSTED", posted_at=now))
    elif kind == "c":
        st = colour_posts.load_state()
        if key in st["groups"]:
            st["groups"][key].update(status="POSTED", posted_at=now)
            colour_posts.save_state(st)
    return f"✅ {MANUAL_KINDS.get(kind, 'Post')} {key} marked as posted."


# --- reels (rendered on this PC) -------------------------------------------------------------------
def reel_buttons(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve (auto-post)", callback_data=f"xq:{rid}"),
         InlineKeyboardButton("🚀 Post now", callback_data=f"xp:{rid}")],
        [InlineKeyboardButton("🎬 Other style", callback_data=f"xt:{rid}"),
         InlineKeyboardButton("🎲 Re-roll", callback_data=f"xr:{rid}")],
        [InlineKeyboardButton("📲 Send file", callback_data=f"xf:{rid}"),
         InlineKeyboardButton("❌ Discard", callback_data=f"xd:{rid}")],
    ])


def reel_times() -> str:
    return " / ".join(reels.auto_cfg().get("post_times") or []) or "the reel times"


def template_buttons(mid: str) -> InlineKeyboardMarkup:
    names = list(reels.templates()["templates"])
    rows = [[InlineKeyboardButton(n.replace("_", " "), callback_data=f"xs:{mid}:{n}") for n in names[i:i + 2]]
            for i in range(0, len(names), 2)]
    return InlineKeyboardMarkup(rows)


async def render_reel(mid: str, template: str | None = None, prow: dict | None = None) -> dict:
    if prow is None:
        prow = await run(lambda p: p.sheet.get(mid) or {})
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_render_executor, lambda: reels.make_reel(mid, prow, template))


async def send_reel_preview(chat, r: dict, prow: dict | None = None, approved=False):
    music = f"🎵 {r['track']}" if r["track"] else "🔇 no music (add tracks to the music folder)"
    title = f"{(prow or {}).get('shopify_title') or r['mid']}"[:60]
    price = f" · ₹{prow['my_price']}" if prow and prow.get("my_price") else ""
    status = f"\n✅ Auto-approved: posts at {reel_times()}" if approved else ""
    with open(r["file"], "rb") as fh:
        await chat.send_video(fh, supports_streaming=True, width=reels.W, height=reels.H,
                              duration=int(r["seconds"]),
                              caption=f"🎬 {title}{price}\n{r['template'].replace('_', ' ')} · {r['seconds']} s · "
                                      f"{music}\nRendered in {r['render_s']} s. ID {r['reel_id']}{status}",
                              reply_markup=None if approved else reel_buttons(r["reel_id"]))


async def make_and_send_reel(chat, mid: str, template: str | None = None):
    note = await chat.send_message(f"🎬 Rendering a {(template or 'reel').replace('_', ' ')} for {mid}… (~30-90 s)")
    try:
        prow = await run(lambda p: p.sheet.get(mid) or {})
        r = await render_reel(mid, template, prow)
    except Exception as e:
        log.exception("reel %s failed", mid)
        await note.edit_text(f"❌ Reel for {mid} failed: {e}")
        return
    try:
        await note.delete()
    except Exception:
        pass
    await send_reel_preview(chat, r, prow)


def reel_post_text(res: dict, mid: str) -> str:
    if res.get("no_link"):
        return f"❌ {mid} isn't Active on the store; /activate it first."
    if res.get("manual"):
        return f"📋 Reel for {mid} is ready to post by hand: file + caption sent below."
    d = res["posted"]
    fb = (f"\n✅ Facebook: {d['fb_permalink']}" if d.get("fb_permalink")
          else f"\n⚠️ Facebook reel failed: {d['fb_error']}" if d.get("fb_error") else "")
    nolink = ("\nℹ️ Product isn't Active yet: caption links to the store home page, DMs say coming soon."
              if d.get("no_product_link") else "")
    return f"✅ Reel posted to Instagram: {d['permalink']}{fb}{nolink}"


# --- daily auto reels ------------------------------------------------------------------------------
AUTO_FILE = reels.DATA_DIR / "reels_auto.json"


def _auto_state() -> dict:
    import json
    return json.loads(AUTO_FILE.read_text(encoding="utf-8")) if AUTO_FILE.exists() else {}


def _save_auto_state(st: dict) -> None:
    import json
    AUTO_FILE.write_text(json.dumps(st, indent=1), encoding="utf-8")


async def run_reel_batch(bot, n: int | None = None) -> str:
    """Render today's reels and send them for approval (or queue them when auto_approve is on)."""
    cfg = reels.auto_cfg()
    n = n or int(cfg.get("per_day", 2))
    chats = [await bot.get_chat(uid) for uid in ALLOWED]
    rows = await run(lambda p: p.sheet.all())
    loop = asyncio.get_running_loop()
    picks = await loop.run_in_executor(_render_executor, lambda: reels.pick_products(rows, n))
    if not picks:
        return "No products to make reels of (all live products had one in the last "  \
               f"{cfg.get('min_days_between', 30)} days)."
    auto = bool(cfg.get("auto_approve"))
    for c in chats:
        await c.send_message(f"🌅 Today's reels ({n}): " + (f"they post at {reel_times()}." if auto else
                             f"tap ✅ Approve on the ones to post; they go out at {reel_times()}."))
    made = 0
    for prow in picks:
        if made >= n:
            break
        mid = str(prow["meesho_id"])
        try:
            r = await render_reel(mid, reels.pick_style(), prow)
        except Exception:
            log.exception("auto reel %s failed", mid)
            continue
        made += 1
        state = reels.load_state()
        state[r["reel_id"]]["auto"] = True
        reels.save_state(state)
        if auto:
            reels.approve(r["reel_id"])
        for c in chats:
            await send_reel_preview(c, r, prow, approved=auto)
    return f"Made {made} reel(s)."


async def job_reel_tick(ctx: ContextTypes.DEFAULT_TYPE):
    """Every 20 min: once a day after render_at (catches up if the PC was off/asleep at that time)."""
    cfg = reels.auto_cfg()
    if not cfg.get("enabled"):
        return
    now = datetime.now(IST)
    h, m = map(int, str(cfg.get("render_at", "07:30")).split(":"))
    st = _auto_state()
    today = now.strftime("%Y-%m-%d")
    if (now.hour, now.minute) < (h, m) or st.get("last_batch") == today:
        return
    st["last_batch"] = today
    _save_auto_state(st)
    try:
        msg = await run_reel_batch(ctx.bot)
        log.info("auto reels: %s", msg)
        if msg.startswith("No products"):
            await notify(ctx.application, "🎬 " + msg)
    except Exception as e:
        log.exception("auto reel batch failed")
        await notify(ctx.application, f"❌ Today's auto reels failed: {e}")


async def job_reel_publish(ctx: ContextTypes.DEFAULT_TYPE):
    if not reels.auto_cfg().get("enabled"):
        return
    rid = reels.next_approved()
    if not rid:
        return
    mid = reels.load_state()[rid]["mid"]
    try:
        res = await run(lambda p: reels.publish(rid, p.sheet, reels_sheet(p)))
        await notify(ctx.application, f"🎬 Auto reel {rid}: " + reel_post_text(res, mid))
        if res.get("manual"):
            await send_manual(ctx.bot, res["manual"])
    except Exception as e:
        log.exception("auto reel publish %s failed", rid)
        await notify(ctx.application, f"❌ Auto reel {rid} failed to post: {e}\nIt stays approved; it's retried "
                                      "at the next reel time (or tap ❌ Discard on it).")


async def cmd_reelbatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else None
    await update.message.reply_text("🎬 Making today's reels now… (~1-2 min each)")
    msg = await run_reel_batch(ctx.bot, n)
    await update.message.reply_text(msg)


async def cmd_music(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    await update.message.reply_text("🎶 Adding new licensed tracks… (a few minutes)")
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(_render_executor, music.fill)
    await update.message.reply_text(music.report(r))


async def job_music(ctx: ContextTypes.DEFAULT_TYPE):
    loop = asyncio.get_running_loop()
    try:
        r = await loop.run_in_executor(_render_executor, music.fill)
        await notify(ctx.application, music.report(r))
    except Exception as e:
        log.exception("weekly music failed")
        await notify(ctx.application, f"❌ Weekly music download failed: {e}")


async def reel_button(chat, q, action: str, rid: str, rest: list):
    state = reels.load_state()
    if action == "xs":   # template picked: rid is the product id here
        await q.edit_message_reply_markup(None)
        await make_and_send_reel(chat, rid, rest[0])
        return
    r = state.get(rid)
    if not r:
        await chat.send_message(f"Reel {rid} not found.")
        return
    if action == "xt":
        await chat.send_message(f"Pick a style for {r['mid']}:", reply_markup=template_buttons(r["mid"]))
    elif action == "xr":
        await make_and_send_reel(chat, r["mid"], r["template"])
    elif action == "xf":   # the file, to post by hand in the Instagram app (e.g. with a trending sound)
        with open(r["file"], "rb") as fh:
            await chat.send_document(fh, filename=f"{rid}.mp4",
                                     caption="Full-quality file: post it from the Instagram app to add a trending "
                                             "sound. (Reels posted by hand aren't tracked for comment→DM.)")
    elif action == "xq":
        if r.get("status") in ("POSTED", "APPROVED"):
            await chat.send_message(f"Reel {rid} is already {r['status'].lower()}.")
            return
        reels.approve(rid)
        await q.edit_message_reply_markup(InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Post now", callback_data=f"xp:{rid}"),
            InlineKeyboardButton("❌ Discard", callback_data=f"xd:{rid}")]]))
        queue = sum(1 for x in reels.load_state().values() if x.get("status") == "APPROVED")
        await chat.send_message(f"✅ Reel {rid} approved: it posts at the next free reel time ({reel_times()}). "
                                f"{queue} in the queue.")
    elif action == "xd":
        await q.edit_message_reply_markup(None)
        state[rid]["status"] = "DISCARDED"
        reels.save_state(state)
        Path(r["file"]).unlink(missing_ok=True)
        await chat.send_message(f"🗑 Reel {rid} discarded.")
    elif action == "xp":
        if r.get("status") == "POSTED":
            await chat.send_message("Already posted.")
            return
        await q.edit_message_reply_markup(None)
        note = await chat.send_message(f"📤 Posting reel {rid} (upload + Instagram processing takes a few minutes)…")
        try:
            res = await run(lambda p: reels.publish(rid, p.sheet, reels_sheet(p)))
        except Exception as e:
            log.exception("reel publish %s failed", rid)
            await note.edit_text(f"❌ Reel post failed: {e}")
            await q.edit_message_reply_markup(reel_buttons(rid))
            return
        await note.edit_text(reel_post_text(res, r["mid"]), disable_web_page_preview=True)
        if res.get("manual"):
            await send_manual(chat.get_bot(), res["manual"])
        if res.get("no_link"):
            await q.edit_message_reply_markup(reel_buttons(rid))


# --- copy a viral reel's structure ---------------------------------------------------------------------
def copied_buttons(name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Try on a product", callback_data=f"vt:{name}"),
                                  InlineKeyboardButton("🗑 Delete style", callback_data=f"vd:{name}")]])


async def copy_viral(chat, video: Path | None = None, url: str | None = None):
    note = await chat.send_message("🔍 Studying this reel: cuts, camera moves, beat, on-screen text… (~1-2 min)")
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(_render_executor,
                                         lambda: viral.copy_link(url) if url else viral.copy_reel(video))
    except Exception as e:
        log.exception("copying reel failed")
        await note.edit_text(f"❌ Couldn't copy that reel: {e}")
        return
    finally:
        if video:
            video.unlink(missing_ok=True)
    try:
        await note.delete()
    except Exception:
        pass
    await chat.send_message(viral.report(res), reply_markup=copied_buttons(res["name"]))


async def on_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """A reel video sent to the bot: copy its structure as a new reel style."""
    if not await authorized(update):
        return
    msg = update.message
    media = msg.video or msg.document
    if media.file_size and media.file_size > 20 * 1024 * 1024:
        await msg.reply_text("That video is over 20 MB (Telegram's limit for bots). Send the reel link "
                             "instead, or a compressed copy.")
        return
    tg_file = await media.get_file()
    tmp = viral.VIRAL_DIR / "_incoming"
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / f"{msg.message_id}.mp4"
    await tg_file.download_to_drive(path)
    await copy_viral(update.effective_chat, video=path)


async def viral_button(chat, q, ctx, action: str, name: str):
    if action == "vt":
        ctx.user_data["try_style"] = name
        await chat.send_message(f"Send the product name or ID to make a {name} reel:",
                                reply_markup=ForceReply(input_field_placeholder="e.g. black bandhani lehenga"))
    elif action == "vd":
        await q.edit_message_reply_markup(None)
        ok = viral.delete_copied(name)
        await chat.send_message(f"🗑 Style {name} deleted." if ok else f"Style {name} was already gone.")


# --- weekly trending songs ----------------------------------------------------------------------------
async def send_songs(bot, chat_ids) -> None:
    loop = asyncio.get_running_loop()
    try:
        r = await loop.run_in_executor(_render_executor, trending.weekly)
        msgs = trending.report(r)
    except Exception as e:
        log.exception("trending songs failed")
        msgs = [f"❌ Weekly trending songs failed: {html.escape(str(e))}"]
    for uid in chat_ids:
        for m in msgs:
            try:
                await bot.send_message(uid, m, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception:
                log.exception("Could not send songs to %s", uid)


async def cmd_songs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    await update.message.reply_text("🎵 Checking this week's charts… (~1 min)")
    await send_songs(ctx.bot, [update.effective_chat.id])


async def job_songs(ctx: ContextTypes.DEFAULT_TYPE):
    await send_songs(ctx.bot, ALLOWED)


async def cmd_reel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    args = list(ctx.args)
    names = list(reels.templates()["templates"])
    template = args.pop() if args and args[-1] in names else None
    if not args:
        n_music = len([p for p in reels.MUSIC_DIR.glob("*") if p.suffix.lower() in reels.AUDIO_EXT]) \
            if reels.MUSIC_DIR.exists() else 0
        a = reels.auto_cfg()
        queue = sum(1 for x in reels.load_state().values() if x.get("status") == "APPROVED")
        auto_line = (f"{a.get('per_day', 2)}/day at {a.get('render_at')}, posting {reel_times()}, "
                     f"{queue} approved in the queue (/reelbatch makes today's now)") if a.get("enabled") else "off"
        tpl = reels.templates()["templates"]
        await update.message.reply_text(
            "Usage: /reel <product ID or title words> [style]\n\nStyles:\n" +
            "\n".join(f"• {n}: {t.get('description', '')}" for n, t in tpl.items()) +
            f"\n\nMusic tracks in the music folder: {n_music} (/music adds more)\n"
            f"Auto reels: {auto_line}\n"
            "To copy a viral reel's style: send me the reel video (or its link).")
        return
    hits = await run(_find_products, " ".join(args))
    if not hits:
        await update.message.reply_text("No product matches that.")
    elif len(hits) == 1:
        await make_and_send_reel(update.effective_chat, str(hits[0]["meesho_id"]), template)
    else:
        rows = [[InlineKeyboardButton(f"{r['shopify_title'] or r['meesho_title']}"[:50] + f" · ₹{r['my_price']}",
                                      callback_data=f"xs:{r['meesho_id']}:{template or names[0]}")] for r in hits[:8]]
        await update.message.reply_text("Which product?", reply_markup=InlineKeyboardMarkup(rows))


# --- handlers ----------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    await update.message.reply_text(
        "Send me Meesho product links (share from the Meesho app works).\n\n"
        "/pending – products waiting for review\n"
        "/show <id> – show a product preview again\n"
        "/price <id> <amount|auto> – override price\n"
        "/activate <id> – make live on the store · /draft <id> – hide it\n"
        "/collection <id> [name] – add to a collection (created if new)\n"
        "/push – push all approved products now\n"
        "/resync – re-check Meesho price/stock now\n"
        "/posts – Instagram post queue · /makeposts [n] – start the Kaggle image batch now\n"
        "/postshow <id> – show a generated post again\n"
        "/postnow – publish the next approved post to Instagram now\n"
        "/originals <name or id> – get a product's photos, make AI images in the Gemini app, "
        "reply with them → carousel post\n"
        "/colours – make this week's colour carousel now (same design in several colours)\n"
        f"/gemini <name or id> – make a single-image post in the Gemini app (Chrome on this PC, style from "
        f"prompts/, max {gemini_web.PER_DAY} a day)\n"
        "/reel <name or id> [style] – make a reel on this PC (preview → post)\n"
        "Send a viral reel (video or Instagram link) – copy its style for /reel\n"
        "/songs – 15 new trending Hindi/Punjabi songs for reels (also sent every Monday)\n"
        "/reelbatch [n] – make today's auto reels now · /music – add licensed tracks now\n"
        "/status – counts by status")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    cap_mid = ctx.user_data.pop("caption_for", None)
    if cap_mid:  # reply to "send the new caption"
        text = update.message.text.strip()
        await run(lambda p: posts_sheet(p).upsert(cap_mid, caption=text))
        o = manual_posts.set_opt(cap_mid, custom_caption=True)
        await update.message.reply_text(f"✏️ Caption saved for {cap_mid}.",
                                        reply_markup=carousel_buttons(cap_mid, bool(o.get("real_first"))))
        return
    style = ctx.user_data.pop("try_style", None)
    if style:  # reply to "send the product name or ID" after copying a reel
        hits = await run(_find_products, update.message.text.strip())
        if not hits:
            await update.message.reply_text("No product matches that.")
        elif len(hits) == 1:
            await make_and_send_reel(update.effective_chat, str(hits[0]["meesho_id"]), style)
        else:
            rows = [[InlineKeyboardButton(f"{r['shopify_title'] or r['meesho_title']}"[:50] + f" · ₹{r['my_price']}",
                                          callback_data=f"xs:{r['meesho_id']}:{style}")] for r in hits[:8]]
            await update.message.reply_text("Which product?", reply_markup=InlineKeyboardMarkup(rows))
        return
    reel_links = [m.group(0) for m in viral.LINK_RE.finditer(update.message.text or "")]
    if reel_links:
        for link in dict.fromkeys(reel_links):
            await copy_viral(update.effective_chat, url=link)
        return
    urls = list(dict.fromkeys(URL_RE.findall(update.message.text or "")))
    if not urls:
        mid = ctx.user_data.pop("new_collection_for", None)
        if mid:  # reply to "send the name of the new collection"
            try:
                title = await run(Pipeline.add_collection_by_title, mid, update.message.text.strip()[:255])
                await update.message.reply_text(f"✅ {mid} added to collection \"{title}\".")
                await show_collections(update.effective_chat, mid)
            except Exception as e:
                await update.message.reply_text(f"❌ Could not create collection: {e}")
            return
        await update.message.reply_text("Send a Meesho product link.")
        return
    who = update.effective_user.username or str(update.effective_user.id)
    chat = update.effective_chat
    for url in urls:
        log.info("Received link from %s: %s", who, url)
        try:  # the status message is cosmetic; never let it stop processing
            status = await update.message.reply_text(
                f"⏳ Scraping + generating listing… (1–3 min)\n{url}", disable_web_page_preview=True)
        except NetworkError:
            status = None
        try:
            summary = await run(Pipeline.process, url, who)
        except Exception as e:
            await chat.send_message(f"❌ Failed: {e}\n{url}", disable_web_page_preview=True)
            continue
        log.info("Processed %s -> %s", summary["meesho_id"], summary["status"])
        if status:
            try:
                await status.delete()
            except Exception:
                pass
        if summary["status"] == "APPROVED":
            await chat.send_message(f"Auto-approved {summary['meesho_id']}.")
            if CFG["pipeline"]["push_on_approve"]:
                await push_and_report(chat, summary["meesho_id"])
        else:
            await send_preview(chat, summary)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    q = update.callback_query
    action, mid, *rest = q.data.split(":")
    chat = update.effective_chat
    try:
        if action == "ct":  # toggle a collection
            title, member = await run(Pipeline.toggle_collection, mid, f"gid://shopify/Collection/{rest[0]}")
            await q.answer(("Added to " if member else "Removed from ") + title)
            await show_collections(chat, mid, message=q.message)
            return
        await q.answer()
        if action == "mk":   # posted by hand in Business Suite
            await q.edit_message_reply_markup(None)
            await chat.send_message(await mark_posted(mid, rest[0]))
        elif action in ("vt", "vd"):
            await viral_button(chat, q, ctx, action, mid)
        elif action in ("xp", "xt", "xr", "xf", "xd", "xs", "xq"):
            await reel_button(chat, q, action, mid, rest)
        elif action in ("kp", "kn", "kx"):   # weekly colour carousel
            await colour_button(chat, q, action, mid)
        elif action in ("gw", "gn"):   # Gemini in Chrome: with the prompts shown / for a picked product
            asyncio.create_task(gemini_make(chat, mid))
        elif action in ("og", "mn", "mr", "me", "mc", "mx", "mq", "mp"):
            await carousel_button(chat, q, ctx, action, mid)
        elif action == "cl":
            await show_collections(chat, mid)
        elif action == "cn":
            ctx.user_data["new_collection_for"] = mid
            await chat.send_message(f"Send the name of the new collection for {mid}:",
                                    reply_markup=ForceReply(input_field_placeholder="e.g. Kids Wear"))
        elif action == "cd":
            await q.edit_message_reply_markup(None)
        elif action in ("ac", "dr"):
            r = await run(Pipeline.set_live, mid, action == "ac")
            await q.edit_message_reply_markup(live_buttons(mid, r["status"]))
            if r["status"] == "ACTIVE":
                await chat.send_message(f"🟢 {mid} is live: {r['url'] or r['admin_url']}")
            else:
                await chat.send_message(f"⏸ {mid} is back to Draft (hidden from the store).")
        elif action in ("pa", "pr", "ps"):
            status = {"pa": "APPROVED", "pr": "QUEUED", "ps": "SKIP"}[action]
            await run(lambda p: posts_sheet(p).upsert(mid, post_status=status))
            await q.edit_message_reply_markup(None)
            await chat.send_message({"pa": f"✅ Post {mid} approved.",
                                     "pr": f"🔄 {mid} goes back in the queue; new images in the next batch.",
                                     "ps": f"⏭ Skipped post {mid}."}[action])
        elif action == "ap":
            await run(Pipeline.set_status, mid, "APPROVED")
            await q.edit_message_reply_markup(None)
            if CFG["pipeline"]["push_on_approve"]:
                await push_and_report(chat, mid)
            else:
                await chat.send_message(f"Approved {mid}. It will be pushed on the next scheduled run.")
        elif action == "rj":
            await run(Pipeline.set_status, mid, "REJECTED")
            await q.edit_message_reply_markup(None)
            await chat.send_message(f"Rejected {mid}.")
        elif action == "rg":
            await q.edit_message_reply_markup(None)
            note = await chat.send_message(f"🔄 Generating new images for {mid}…")
            summary = await run(Pipeline.regenerate_images, mid)
            await note.delete()
            await send_preview(chat, summary)
    except Exception as e:
        log.exception("Button %s failed", q.data)
        await chat.send_message(f"❌ {action} failed for {mid}: {e}")


async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    if len(ctx.args) != 2:
        await update.message.reply_text("Usage: /price <id> <amount>  or  /price <id> auto")
        return
    mid, amount = ctx.args[0].lower(), ctx.args[1].lower()
    try:
        price = None if amount == "auto" else float(amount.replace("₹", ""))
        summary = await run(Pipeline.set_price, mid, price)
        await send_preview(update.effective_chat, summary)
        if summary["status"] == "PUSHED":
            await update.message.reply_text("This product is live; tap Approve to push the new price.")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_live(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    active = update.message.text.lstrip("/").lower().startswith("activate")
    if not ctx.args:
        await update.message.reply_text(f"Usage: /{'activate' if active else 'draft'} <id>")
        return
    try:
        r = await run(Pipeline.set_live, ctx.args[0].lower(), active)
        await update.message.reply_text(
            f"🟢 Live: {r['url'] or r['admin_url']}" if active else "⏸ Back to Draft.",
            reply_markup=live_buttons(r["meesho_id"], r["status"]))
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_collection(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /collection <id> [collection name]  (no name = show picker)")
        return
    mid = ctx.args[0].lower()
    try:
        if len(ctx.args) == 1:
            await show_collections(update.effective_chat, mid)
            return
        title = await run(Pipeline.add_collection_by_title, mid, " ".join(ctx.args[1:]))
        await update.message.reply_text(f"✅ {mid} added to collection \"{title}\".")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /show <id>")
        return
    try:
        await send_preview(update.effective_chat, await run(Pipeline.summary, ctx.args[0].lower()))
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    rows = await run(lambda p: p.sheet.by_status("READY"))
    if not rows:
        await update.message.reply_text("Nothing waiting for review.")
        return
    lines = [f"<code>{esc(r['meesho_id'])}</code> ₹{esc(r['my_price'])} – {esc(r['shopify_title'])}" for r in rows]
    await update.message.reply_text("Waiting for review (use /show &lt;id&gt;):\n" + "\n".join(lines),
                                    parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    counts = await run(lambda p: p.sheet.counts())
    await update.message.reply_text("\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "Sheet is empty.")


async def cmd_push(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    await update.message.reply_text("⬆️ Pushing approved products…")
    await report_push(ctx.application, force_message=True)


async def cmd_resync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    await update.message.reply_text("🔁 Re-checking all live products on Meesho…")
    await report_resync(ctx.application, force_message=True)


async def cmd_posts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    rows = await run(lambda p: posts_sheet(p).all())
    counts = {}
    for r in rows:
        counts[r["post_status"]] = counts.get(r["post_status"], 0) + 1
    batch = kaggle_posts.current_batch()
    state = (f"Kaggle batch: {len(batch['posts'])} posts, started {batch.get('pushed', 'not pushed')}"
             if batch else "No Kaggle batch running.")
    gen = [str(r["meesho_id"]) for r in rows if r["post_status"] == "GENERATED"]
    text = "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) + f"\n\n{state}"
    if gen:
        text += f"\nWaiting for review: {', '.join(gen[:20])} (/postshow <id>)"
    await update.message.reply_text(text)


async def cmd_postshow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /postshow <id>")
        return
    await send_post_preview(update.effective_chat, ctx.args[0].lower())


async def cmd_postnow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    await update.message.reply_text("📤 Publishing the next approved post…")
    await update.message.reply_text(await publish_next_post(ctx.bot))


async def cmd_originals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /originals <product ID or words from the title>\n"
                                        "e.g. /originals 4k2x9p  or  /originals black bandhani lehenga")
        return
    hits = await run(_find_products, " ".join(ctx.args))
    if not hits:
        await update.message.reply_text("No product matches that. Try the product ID or fewer words.")
    elif len(hits) == 1:
        await send_originals(update.effective_chat, str(hits[0]["meesho_id"]))
    else:
        rows = [[InlineKeyboardButton(f"{r['shopify_title'] or r['meesho_title']}"[:50] + f" · ₹{r['my_price']}",
                                      callback_data=f"og:{r['meesho_id']}")] for r in hits[:8]]
        more = f" (showing 8 of {len(hits)}; add words to narrow it down)" if len(hits) > 8 else ""
        await update.message.reply_text(f"Which product?{more}", reply_markup=InlineKeyboardMarkup(rows))


async def cmd_makeposts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else None
    await update.message.reply_text(await start_post_batch(n))


# --- scheduled jobs ------------------------------------------------------------
def _start_batch(p: Pipeline, n: int | None) -> str:
    from post import queue as queue_posts
    if kaggle_posts.current_batch():
        return "A Kaggle batch is already running; results come automatically."
    ps = posts_sheet(p)
    added = queue_posts(p.sheet, ps)
    rows = ps.next("QUEUED", n or styles()["posts_per_day"])
    if not rows:
        return "No QUEUED posts left in the Posts tab."
    batch = kaggle_posts.prepare(rows, ps.recent_scenes(), p.sheet)
    for mid in batch["posts"]:
        ps.upsert(mid, post_status="GENERATING", error="")
    kaggle_posts.push()
    n_img = sum(len(v["shots"]) for v in batch["posts"].values())
    return (f"🎨 Kaggle GPU batch started: {len(batch['posts'])} posts ({n_img} images, ~7 min each)."
            + (f" Queued {added} new products." if added else ""))


async def start_post_batch(n: int | None = None) -> str:
    try:
        return await run(_start_batch, n)
    except Exception as e:
        log.exception("Kaggle batch start failed")
        return f"❌ Could not start the Kaggle batch: {e}"


async def job_post_batch(ctx: ContextTypes.DEFAULT_TYPE):
    await notify(ctx.application, await start_post_batch())


async def publish_next_post(bot) -> str:
    try:
        res = await run(lambda p: publisher.publish_next(p.sheet, posts_sheet(p)))
    except Exception as e:
        log.exception("Instagram publish failed")
        return f"❌ Instagram post failed: {e}"
    if res.get("manual"):
        await send_manual(bot, res["manual"])
    return publish_report(res)


def publish_report(res: dict) -> str:
    waiting = (f"\n⚠️ Approved but product not Active (no store link): {', '.join(res['skipped'])}"
               if res.get("skipped") else "")
    if res.get("manual"):
        return f"📋 {res['mid']} is ready to post by hand: files + caption sent.{waiting}"
    if res.get("posted"):
        d = res["posted"]
        fb = (f"\n✅ Facebook: {d['fb_permalink']}" if d.get("fb_permalink")
              else f"\n⚠️ Facebook post failed: {d['fb_error']}" if d.get("fb_error") else "")
        nolink = ("\nℹ️ Product isn't Active yet: the post links to the store home page and comment DMs say "
                  f"\"coming soon\" until you /activate {d['mid']}." if d.get("no_product_link") else "")
        return f"✅ Posted {d['mid']} to Instagram: {d['permalink']}{fb}{nolink}{waiting}"
    if res.get("error"):
        return f"❌ Instagram post failed: {res['error']}{waiting}"
    return f"Nothing posted: {res.get('reason')}.{waiting}"


async def job_publish(ctx: ContextTypes.DEFAULT_TYPE):
    msg = await publish_next_post(ctx.bot)
    if not msg.startswith("Nothing posted: no APPROVED posts."):
        await notify(ctx.application, msg)


async def job_comments(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        done = await run(lambda p: publisher.check_comments(p.sheet, posts_sheet(p), reels_sheet(p)))
    except Exception as e:
        log.warning("Comment check failed: %s", e)
        return
    failed = [d for d in done if d.get("dm") != "sent"]
    if failed:
        await notify(ctx.application, "⚠️ Could not DM some commenters:\n" +
                     "\n".join(f"[{d['platform']}] {d['username']}: {d['dm']}" for d in failed[:10]))
    if done:
        log.info("Answered %d comments (%d Instagram, %d Facebook)", len(done),
                 sum(d["platform"] == "instagram" for d in done), sum(d["platform"] == "facebook" for d in done))


async def job_ig_token(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        log.info(await run(lambda p: instagram.refresh_token()))
    except Exception as e:
        await notify(ctx.application, f"⚠️ Instagram token refresh failed: {e}")


async def job_post_check(ctx: ContextTypes.DEFAULT_TYPE):
    """Every 10 minutes: when the Kaggle batch is done, fetch it and send the previews."""
    batch = kaggle_posts.current_batch()
    if not batch or not batch.get("pushed"):
        return
    try:
        st = await run(lambda p: kaggle_posts.status())
    except Exception as e:
        log.warning("Kaggle status failed: %s", e)
        return
    if st in ("RUNNING", "QUEUED"):
        return
    try:
        res = await run(lambda p: kaggle_posts.fetch(posts_sheet(p), p.sheet))
    except Exception as e:
        log.exception("Kaggle fetch failed")
        await notify(ctx.application, f"❌ Kaggle batch finished ({st}) but fetching failed: {e}")
        return
    msg = f"🎨 Kaggle batch {st}: {len(res['ok'])} posts ready for review"
    if res["failed"]:
        msg += f", {len(res['failed'])} back in the queue ({', '.join(res['failed'])})"
    await notify(ctx.application, msg)
    for uid in ALLOWED:
        chat = await ctx.application.bot.get_chat(uid)
        for mid in res["ok"]:
            try:
                await send_post_preview(chat, mid)
            except Exception:
                log.exception("Post preview %s failed", mid)



async def notify(app: Application, text: str):
    for uid in ALLOWED:
        try:
            await app.bot.send_message(uid, text[:4000], disable_web_page_preview=True)
        except Exception:
            log.exception("Could not notify %s", uid)


async def report_push(app: Application, force_message=False):
    done, failed = await run(Pipeline.push_approved)
    if done or failed or force_message:
        lines = [f"✅ {d['meesho_id']}: {d['admin_url']}" for d in done] + [f"❌ {f}" for f in failed]
        await notify(app, "Push results:\n" + ("\n".join(lines) or "Nothing approved to push."))


SYNC_FILE = reels.DATA_DIR / "sync_summary.json"
SUMMARY_AT = dtime(21, 0, tzinfo=IST)


def tally_changes(changes: list[str], checked: int = 0) -> None:
    """Count Meesho changes for the daily summary instead of messaging each one."""
    import json
    st = json.loads(SYNC_FILE.read_text(encoding="utf-8")) if SYNC_FILE.exists() else {}
    st.setdefault("checked", 0)
    for k in ("price", "stock", "removed", "deleted", "override"):
        st.setdefault(k, [])
    st["checked"] += checked
    for msg in changes:
        for line in str(msg).splitlines():
            mid, _, what = line.partition(": ")
            if "deleted in Shopify" in what:
                kind = ["deleted"]
            elif "removed from Meesho" in what:
                kind = ["removed"]
            elif "override" in what:
                kind = ["override"]
            elif "could not check" in what or what.startswith("error"):
                continue
            else:
                kind = []
                for part in what.split("; "):
                    b, _, a = part.partition(" → ")
                    pb, pa = re.findall(r"₹([\d.]+)", b), re.findall(r"₹([\d.]+)", a)
                    if pb != pa:
                        kind.append("price")
                    if ("OOS" in b) != ("OOS" in a) or not pb or not pa:
                        kind.append("stock")
            for k in set(kind):
                if mid not in st[k]:
                    st[k].append(mid)
    SYNC_FILE.write_text(json.dumps(st), encoding="utf-8")


async def job_sync_summary(ctx: ContextTypes.DEFAULT_TYPE):
    """Once a day: how many products the Meesho sync updated (no per-product messages)."""
    import json
    if not SYNC_FILE.exists():
        return
    st = json.loads(SYNC_FILE.read_text(encoding="utf-8"))
    SYNC_FILE.unlink()
    updated = len(set(st["price"]) | set(st["stock"]) | set(st["removed"]) | set(st["deleted"]))
    parts = [f"{len(st[k])} {label}" for k, label in (("price", "price changes"), ("stock", "stock changes"),
                                                       ("removed", "removed from Meesho → Draft"),
                                                       ("deleted", "deleted in Shopify")) if st[k]]
    text = (f"🔄 Meesho sync today: {st['checked']} checks, {updated} products updated on the store"
            + (f" ({', '.join(parts)})." if parts else "."))
    if st["override"]:   # these lose money, so they're named
        text += f"\n⚠️ Your price override is now below Meesho's cost for: {', '.join(st['override'][:20])}"
    await notify(ctx.application, text)


async def report_resync(app: Application, force_message=False):
    changes = await run(Pipeline.resync)
    if force_message:
        await notify(app, f"Meesho resync done: {len(changes)} products changed (details in today's 9 PM summary).")
    tally_changes(changes)


async def job_push(ctx: ContextTypes.DEFAULT_TYPE):
    await report_push(ctx.application)


async def job_resync(ctx: ContextTypes.DEFAULT_TYPE):
    await report_resync(ctx.application)


_blocked_notified = {"day": None}
_copy_state = {"paused_until": 0.0, "done_today": 0, "day": None}


async def job_upgrade_copy(ctx: ContextTypes.DEFAULT_TYPE):
    """Every 15 min: replace template listing text with AI text, a few products at a time, until the
    daily Gemini quota runs out (then pause until it resets, ~13:30 IST)."""
    now_ist = datetime.now(IST)
    if _copy_state["day"] != now_ist.date():
        if _copy_state["day"] is not None and _copy_state["done_today"]:
            log.info("AI copy upgrades yesterday: %d", _copy_state["done_today"])
        _copy_state.update(day=now_ist.date(), done_today=0)
    if now_ist.timestamp() < _copy_state["paused_until"]:
        return
    try:
        res = await run(Pipeline.upgrade_copy, int(CFG["pipeline"].get("copy_upgrade_batch", 6)))
    except Exception as e:
        log.warning("Copy upgrade failed: %s", e)
        return
    _copy_state["done_today"] += len(res["upgraded"])
    if res["upgraded"]:
        log.info("AI copy: upgraded %s (%d template listings left)", ", ".join(res["upgraded"]), res["left"])
    if res["quota_out"] or not res["left"]:
        # Gemini free quotas reset at midnight Pacific (13:30 IST in winter, 12:30 in summer)
        reset = now_ist.replace(hour=13, minute=45, second=0, microsecond=0)
        if reset <= now_ist:
            reset += timedelta(days=1)
        _copy_state["paused_until"] = reset.timestamp()
        log.info("AI copy upgrade paused until %s (%s)", reset.strftime("%d %b %H:%M"),
                 "quota used up" if res["quota_out"] else "nothing left")


async def job_resync_rolling(ctx: ContextTypes.DEFAULT_TYPE):
    """Every few minutes: re-check the products checked longest ago (each ~every N hours)."""
    p = CFG["pipeline"]
    try:
        res = await run(Pipeline.resync_due, float(p.get("resync_every_hours", 2)),
                        int(p.get("resync_batch_size", 10)))
    except Exception as e:
        log.warning("Rolling resync failed: %s", e)
        return
    if res["checked"] or res["errors"]:
        log.info("Rolling resync: checked %d, %d changes, %d errors", res["checked"], len(res["changes"]),
                 len(res["errors"]))
    tally_changes(res["changes"], res["checked"])   # reported once a day by job_sync_summary
    today = datetime.now(IST).date()
    if res["blocked"] and _blocked_notified["day"] != today:
        _blocked_notified["day"] = today
        await notify(ctx.application, "⚠️ Meesho is blocking the price/stock checker right now; it will keep "
                                      "retrying every few minutes. If it lasts for hours, check WARP/Chrome.")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    if update is None and isinstance(ctx.error, NetworkError):  # polling hiccup; it retries itself
        log.warning("Telegram network error (will retry): %s", ctx.error)
        return
    text = getattr(getattr(update, "effective_message", None), "text", None)
    log.error("Handler failed for message %r", text, exc_info=ctx.error)


def main():
    # Some Indian ISPs drop many connections to api.telegram.org; use generous timeouts
    # and keep retrying rather than exiting.
    app = (Application.builder().token(env("TELEGRAM_BOT_TOKEN"))
           .request(RetryingRequest(connect_timeout=30, read_timeout=30, write_timeout=60, pool_timeout=30))
           .get_updates_request(HTTPXRequest(connect_timeout=30, read_timeout=60, pool_timeout=30))
           .build())
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler(["activate", "draft"], cmd_live))
    app.add_handler(CommandHandler("collection", cmd_collection))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("push", cmd_push))
    app.add_handler(CommandHandler("resync", cmd_resync))
    app.add_handler(CommandHandler("posts", cmd_posts))
    app.add_handler(CommandHandler("postshow", cmd_postshow))
    app.add_handler(CommandHandler("makeposts", cmd_makeposts))
    app.add_handler(CommandHandler("postnow", cmd_postnow))
    app.add_handler(CommandHandler("originals", cmd_originals))
    app.add_handler(CommandHandler("gemini", cmd_gemini))
    app.add_handler(CommandHandler("colours", cmd_colours))
    app.add_handler(CommandHandler("colors", cmd_colours))
    app.add_handler(CommandHandler(["reel", "reels"], cmd_reel))
    app.add_handler(CommandHandler("songs", cmd_songs))
    app.add_handler(CommandHandler("reelbatch", cmd_reelbatch))
    app.add_handler(CommandHandler("music", cmd_music))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.add_error_handler(on_error)

    p = CFG["pipeline"]
    if p.get("push_every_minutes"):
        app.job_queue.run_repeating(job_push, interval=p["push_every_minutes"] * 60, first=120)
    if p.get("copy_upgrade_batch"):
        app.job_queue.run_repeating(job_upgrade_copy, interval=15 * 60, first=600)
    if p.get("resync_every_hours"):
        app.job_queue.run_repeating(job_resync_rolling, interval=int(p.get("resync_check_every_minutes", 10)) * 60,
                                    first=300)
    if p.get("resync_daily_at"):
        h, m = map(int, str(p["resync_daily_at"]).split(":"))
        app.job_queue.run_daily(job_resync, time=dtime(h, m, tzinfo=IST))

    k = styles().get("kaggle", {})
    if k.get("run_at"):
        h, m = map(int, str(k["run_at"]).split(":"))
        app.job_queue.run_daily(job_post_batch, time=dtime(h, m, tzinfo=IST))
    app.job_queue.run_repeating(job_post_check, interval=600, first=60)
    cc = styles().get("colour_carousel") or {}
    if cc.get("enabled"):
        h, m = map(int, str(cc.get("at", "11:00")).split(":"))
        day = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"].index(
            str(cc.get("day", "sunday")).lower())
        app.job_queue.run_daily(job_colour_post, time=dtime(h, m, tzinfo=IST), days=(day,))
    g = styles().get("gemini_web") or {}
    if g.get("enabled"):
        for t in g.get("run_at") or []:
            h, m = map(int, str(t).split(":"))
            app.job_queue.run_daily(job_gemini_auto, time=dtime(h, m, tzinfo=IST))
    app.job_queue.run_daily(job_songs, time=dtime(10, 0, tzinfo=IST), days=(1,))   # Mondays 10:00
    app.job_queue.run_daily(job_music, time=dtime(9, 30, tzinfo=IST), days=(0,))   # Sundays 9:30
    app.job_queue.run_repeating(job_reel_tick, interval=20 * 60, first=120)
    app.job_queue.run_daily(job_sync_summary, time=SUMMARY_AT)
    for t in reels.auto_cfg().get("post_times") or []:
        h, m = map(int, str(t).split(":"))
        app.job_queue.run_daily(job_reel_publish, time=dtime(h, m, tzinfo=IST))
    manual = publisher.manual_mode()
    st = styles()
    if manual or env("IG_ACCESS_TOKEN", required=False):
        for t in st.get("post_times") or []:
            h, m = map(int, str(t).split(":"))
            app.job_queue.run_daily(job_publish, time=dtime(h, m, tzinfo=IST))
    if not manual and env("IG_ACCESS_TOKEN", required=False):
        every = int(st.get("comment_automation", {}).get("check_every_minutes", 2))
        app.job_queue.run_repeating(job_comments, interval=every * 60, first=90)
        app.job_queue.run_repeating(job_ig_token, interval=7 * 86400, first=3600)

    if not ALLOWED:
        log.warning("TELEGRAM_ALLOWED_USER_IDS is empty; send /start to the bot to get your ID")
    log.info("Bot running. Press Ctrl+C to stop.")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1, timeout=50)
    finally:
        if _pipeline:
            _executor.submit(_pipeline.close).result(timeout=30)


if __name__ == "__main__":
    main()
