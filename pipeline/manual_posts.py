"""Carousels made by hand: the bot sends a product's original photos, you make AI images in the Gemini
app, send them back to the bot, and it posts them like any other post (same Posts tab, same publisher,
same comment->DM link).

Uploads are kept in images/<id>/post/src/; the carousel files (01-manual.jpg, ...) are rebuilt from
them whenever the draft changes. Draft state (which Telegram message belongs to which product) lives
in data/carousel_drafts.json so it survives bot restarts.
"""
import json
import shutil
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

from .config import DATA_DIR
from .posts import audience, caption, plan, post_dir, post_images, post_without_link, product_link, store_url

DRAFTS_FILE = DATA_DIR / "carousel_drafts.json"
MAX_SLIDES = 10   # Instagram carousel limit (Content Publishing API)
FEED_SIZE = (1080, 1350)   # 4:5 portrait, the tallest Instagram feed shape


# --- draft state ----------------------------------------------------------------------------------
def _load() -> dict:
    if DRAFTS_FILE.exists():
        return json.loads(DRAFTS_FILE.read_text(encoding="utf-8"))
    return {"last": "", "msgs": {}, "opts": {}}


def _save(state: dict) -> None:
    state["msgs"] = dict(list(state["msgs"].items())[-300:])   # keep the file small
    DRAFTS_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def open_draft(mid: str, prev_status: str) -> None:
    """Start (or restart) a draft: forget earlier uploads for this product."""
    state = _load()
    state["last"] = mid
    state["opts"][mid] = {"real_first": False, "custom_caption": False, "prev_status": prev_status}
    _save(state)
    shutil.rmtree(src_dir(mid), ignore_errors=True)


def remember_messages(mid: str, message_ids: list[int]) -> None:
    """Replies to any of these messages go to this product's draft."""
    state = _load()
    state["last"] = mid
    state["msgs"].update({str(m): mid for m in message_ids})
    _save(state)


def draft_for(reply_to: int | None, caption_text: str, known_ids: set[str]) -> str:
    """Which product an incoming album belongs to: the replied-to message, else a product ID in the
    caption, else the most recent draft."""
    state = _load()
    if reply_to and str(reply_to) in state["msgs"]:
        return state["msgs"][str(reply_to)]
    for word in caption_text.lower().replace(",", " ").split():
        if word in known_ids:
            return word
    return state["last"] if state["last"] in state["opts"] else ""


def opts(mid: str) -> dict:
    return _load()["opts"].get(mid) or {"real_first": False, "custom_caption": False, "prev_status": ""}


def set_opt(mid: str, **values) -> dict:
    state = _load()
    state["opts"].setdefault(mid, opts(mid)).update(values)
    _save(state)
    return state["opts"][mid]


def close_draft(mid: str) -> None:
    state = _load()
    state["opts"].pop(mid, None)
    if state["last"] == mid:
        state["last"] = ""
    state["msgs"] = {k: v for k, v in state["msgs"].items() if v != mid}
    _save(state)


# --- prompts for the Gemini app -------------------------------------------------------------------
def prompts(mid: str, prow: dict, recent_scenes: list[str]) -> list[dict]:
    """One ready-to-paste prompt per slide, from the same shot plan as the Kaggle posts (scenes not
    used recently, same model across the carousel, face hidden, outfit locked to the reference)."""
    shots = plan(mid, audience(mid, prow), recent_scenes)
    for s in shots:
        s["prompt"] = s["prompt"].replace("vertical 3:4", "vertical 4:5 portrait")
    post_dir(mid).mkdir(parents=True, exist_ok=True)   # recorded so later posts pick other scenes
    (post_dir(mid) / "plan.json").write_text(json.dumps(shots, indent=1, ensure_ascii=False), encoding="utf-8")
    return shots


# --- images ---------------------------------------------------------------------------------------
def src_dir(mid: str) -> Path:
    return post_dir(mid) / "src"


def uploads(mid: str) -> list[Path]:
    return sorted(src_dir(mid).glob("*.jpg"))


def add_upload(mid: str, data: bytes) -> Path:
    """Save one image from Telegram (any format) as the next upload."""
    from io import BytesIO
    d = src_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{len(uploads(mid)) + 1:02d}.jpg"
    ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB").save(out, quality=95)
    return out


def fit_feed(src: Path, dst: Path, size=FEED_SIZE) -> Path:
    """Fit the whole image into 4:5 without cutting anything off: near-4:5 images are just resized,
    others sit on a blurred, darkened copy of themselves (so the garment is never cropped)."""
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    w, h = im.size
    W, H = size
    if abs((h / w) / (H / W) - 1) < 0.03:
        out = im.resize(size, Image.LANCZOS)
    else:
        bg = ImageOps.fit(im, size, Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
        bg = Image.blend(bg, Image.new("RGB", size, (0, 0, 0)), 0.25)
        scale = min(W / w, H / h)
        fg = im.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        bg.paste(fg, ((W - fg.size[0]) // 2, (H - fg.size[1]) // 2))
        out = bg
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, "JPEG", quality=95)
    return dst


def build(mid: str, real_photo: Path | None) -> list[Path]:
    """(Re)build the carousel files from the uploads, optionally with the real photo first."""
    out_dir = post_dir(mid)
    for old in post_images(mid):
        old.unlink()
    shutil.rmtree(out_dir / "ig", ignore_errors=True)   # cached 4:5 copies of the previous images
    sources = ([real_photo] if real_photo else []) + uploads(mid)
    return [fit_feed(s, out_dir / f"{i:02d}-manual.jpg") for i, s in enumerate(sources[:MAX_SLIDES], 1)]


def clear(mid: str) -> None:
    shutil.rmtree(src_dir(mid), ignore_errors=True)
    for old in post_images(mid):
        old.unlink()


# --- Posts tab ------------------------------------------------------------------------------------
def save_post(mid: str, prow: dict, posts, files: list[Path]) -> dict:
    """Write the Posts row as a GENERATED manual post and return {caption, link, warning}."""
    aud = audience(mid, prow)
    link = product_link(prow)
    o = opts(mid)
    old = next((r for r in posts.all() if str(r["meesho_id"]) == mid), {})
    # saved with a {link} placeholder: filled in when posting (product link, or the store if not Active)
    text = old.get("caption") if o.get("custom_caption") and old.get("caption") else \
        caption(mid, aud, prow.get("my_price", ""), "{link}")
    posts.upsert(mid, post_status="GENERATED", source="manual", title=prow.get("shopify_title", ""),
                 audience=aud, price=prow.get("my_price", ""), link=link, caption=text,
                 images=", ".join(f.name for f in files), shots="manual (Gemini app)", error="")
    warning = ""
    if not link and post_without_link():
        warning = (f"Product isn't Active on the store yet: the post links to the store home page and \"Link\" "
                   f"commenters get the coming-soon DM until you /activate {mid}.")
    elif not link:
        warning = f"Product isn't Active on the store yet, so there's no link to post. Use /activate {mid} first."
    elif str(o.get("prev_status")) == "POSTED":
        warning = ("This product was posted before; the new post replaces it for comment→DM tracking "
                   "(\"Link\" comments on the old post stop getting DMs).")
    shown = str(text).replace("{link}", link or store_url())
    return {"caption": shown, "link": link, "warning": warning}
