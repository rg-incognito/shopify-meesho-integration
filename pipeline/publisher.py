"""Posts APPROVED rows of the Posts tab to Instagram and the Facebook Page (top to bottom) and answers
"Link" comments on both."""
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import facebook, instagram
from .config import DATA_DIR
from .posts import audience, caption, post_dir, post_images, post_without_link, product_link, store_url, styles
from .shopify import Shopify

log = logging.getLogger(__name__)

READY_DIR = DATA_DIR / "manual_ready"


def manual_mode() -> bool:
    """publish_mode: manual in post_styles.yaml: hand posts over for Meta Business Suite, no API calls."""
    return str(styles().get("publish_mode", "api")).strip().lower() == "manual"


def export_ready(kind: str, key: str, files: list, text: str) -> dict:
    """Copies a post's files + caption to data/manual_ready/<date>/<kind>-<key>/ for posting by hand.
    kind: "p" carousel post, "r" reel, "c" colour carousel."""
    d = READY_DIR / datetime.now().strftime("%Y-%m-%d") / f"{kind}-{key}"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i, f in enumerate(files, 1):
        dst = d / f"{i:02d}{Path(f).suffix}"
        shutil.copyfile(f, dst)
        out.append(str(dst))
    (d / "caption.txt").write_text(text, encoding="utf-8")
    return {"kind": kind, "key": key, "files": out, "caption": text, "folder": str(d)}


def publish_next(products, posts) -> dict:
    """Publishes the first APPROVED post (with no_store_link.post_anyway off: whose product is live).
    Returns {"posted": row-info} or {"skipped": [...], "reason": ...}."""
    skipped = []
    for row in posts.next("APPROVED", 1000):
        res = publish(row, products, posts)
        if res.get("no_link"):
            skipped.append(str(row["meesho_id"]))
            continue
        if res.get("no_images"):
            continue
        return {**res, "skipped": skipped}
    return {"skipped": skipped, "reason": "no APPROVED post with an Active product" if skipped
            else "no APPROVED posts"}


def post_caption(row: dict, aud: str, prow: dict, link: str) -> str:
    """Manual posts keep the caption saved with them (it may have been edited in Telegram)."""
    mid = str(row["meesho_id"])
    if row.get("source") == "manual" and row.get("caption"):
        return str(row["caption"]).replace("(link coming soon)", link).replace("{link}", link)
    return caption(mid, aud, prow.get("my_price", ""), link)


def publish(row: dict, products, posts) -> dict:
    """Publishes one Posts row to Instagram (+ Facebook). Returns {"posted": ...}, {"error": ...},
    {"no_link": True} (product not Active and no_store_link.post_anyway off) or {"no_images": True}.
    A product that isn't Active is posted with the store home page as the link; the row's `link` stays
    empty so comment DMs switch to the product link once it goes live."""
    mid = str(row["meesho_id"])
    prow = products.get(mid) or {}
    link = product_link(prow)
    if not link and not post_without_link():
        return {"no_link": True}
    imgs = post_images(mid)
    if not imgs:
        if row.get("source") == "manual":
            posts.upsert(mid, post_status="ERROR", error="carousel images missing; send them again")
        else:
            posts.upsert(mid, post_status="QUEUED", error="images missing; regenerating")
        return {"no_images": True}
    aud = row.get("audience") or audience(mid, prow)
    text = post_caption(row, aud, prow, link or store_url())
    if manual_mode():
        files = [instagram.to_feed_ratio(f, post_dir(mid) / "ig" / f.name) for f in imgs]
        posts.upsert(mid, post_status="READY", link=link, caption=text, error="")
        return {"manual": export_ready("p", mid, files, text), "mid": mid, "title": row["title"]}
    try:
        shop = Shopify()
        urls = [shop.host_image(instagram.to_feed_ratio(f, post_dir(mid) / "ig" / f.name), alt=row["title"])
                for f in imgs]
        res = instagram.publish_carousel(urls, text)
    except Exception as e:
        log.exception("publish %s failed", mid)
        posts.upsert(mid, error=f"publish: {e}"[:300])
        return {"error": f"{mid}: {e}"}
    posts.upsert(mid, post_status="POSTED", link=link, caption=text, ig_media_id=res["media_id"],
                 ig_permalink=res["permalink"], posted_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                 error="", fb_post_id="", fb_permalink="")
    if facebook_enabled():
        try:
            fb = facebook.publish_photos(urls, text)
            posts.upsert(mid, fb_post_id=fb["post_id"], fb_permalink=fb["permalink"])
            res["fb_permalink"] = fb["permalink"]
        except Exception as e:   # Instagram post stands; report the Facebook failure
            log.exception("facebook publish %s failed", mid)
            posts.upsert(mid, error=f"facebook: {e}"[:300])
            res["fb_error"] = str(e)
    return {"posted": {"mid": mid, "title": row["title"], "no_product_link": not link, **res}}


def facebook_enabled() -> bool:
    return bool(styles().get("facebook", {}).get("enabled", True)) and facebook.configured()


_live_links: dict[str, tuple[float, str]] = {}   # mid -> (checked at, product link or "")


def live_link(mid: str, prow: dict, posts) -> str:
    """Product link for a post made before the product was Active (checked every 30 min)."""
    at, link = _live_links.get(mid, (0.0, ""))
    if time.time() - at > 1800:
        link = product_link(prow)
        _live_links[mid] = (time.time(), link)
        if link and posts is not None:
            posts.upsert(mid, link=link)
    return link


def check_comments(products, posts, reels=None) -> list[dict]:
    """Answers keyword comments on posts and reels from the last 7 days, on Instagram and the Facebook
    Page. Each result has "platform" ("instagram" / "facebook")."""
    cfg = styles()["comment_automation"]
    cutoff = datetime.now() - timedelta(days=7)
    ig_recent, fb_recent, by_id = [], [], {}
    sources = [(posts, "meesho_id", "post_status", posts.all())]
    if reels is not None:
        sources.append((reels, "reel_id", "status", reels.all()))
    for sheet, key, status_col, rows in sources:
        for r in rows:
            if r[status_col] != "POSTED":
                continue
            try:
                when = datetime.strptime(str(r.get("posted_at")), "%Y-%m-%d %H:%M")
            except ValueError:
                when = datetime.now()
            if when < cutoff:
                continue
            mid = str(r["meesho_id"])
            prow = products.get(mid) or {}
            info = {"title": r["title"], "link": r.get("link", ""),
                    "price": prow.get("my_price") or r.get("price", "")}
            if not info["link"]:   # posted before the product was Active
                info["link"] = live_link(mid, prow, posts if sheet is posts else None)
                if not info["link"]:
                    info.update(link=store_url(), dm_template=(styles().get("no_store_link") or {}).get("dm_template"))
            target = (sheet, str(r[key]), r)
            if r.get("ig_media_id"):
                ig_recent.append({"media_id": str(r["ig_media_id"]), **info})
                by_id[str(r["ig_media_id"])] = target
            if r.get("fb_post_id"):
                fb_recent.append({"post_id": str(r["fb_post_id"]), **info})
                by_id[str(r["fb_post_id"])] = target
    from .colour_posts import CommentTargets
    colours = CommentTargets(products)
    for r in colours.recent():          # weekly colour carousels: the DM lists every colour's link
        info = {k: r[k] for k in ("title", "link", "price", "dm_template")}
        target = (colours, r["gid"], r)
        if r.get("ig_media_id"):
            ig_recent.append({"media_id": str(r["ig_media_id"]), **info})
            by_id[str(r["ig_media_id"])] = target
        if r.get("fb_post_id"):
            fb_recent.append({"post_id": str(r["fb_post_id"]), **info})
            by_id[str(r["fb_post_id"])] = target
    done = []
    if ig_recent:
        done += [{**d, "platform": "instagram"}
                 for d in instagram.handle_comments(ig_recent, cfg, instagram.me()["username"])]
    if fb_recent and facebook_enabled():
        try:
            done += [{**d, "platform": "facebook"} for d in facebook.handle_comments(fb_recent, cfg)]
        except facebook.FacebookError as e:   # don't let a Facebook problem block Instagram replies
            log.warning("facebook comments: %s", e)
    for d in done:
        sheet, key, r = by_id[d.get("media_id") or d["post_id"]]
        if d.get("dm") == "sent":
            r["dms_sent"] = int(r.get("dms_sent") or 0) + 1
            sheet.upsert(key, dms_sent=r["dms_sent"])
    return done
