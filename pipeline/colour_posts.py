"""Weekly "same design, several colours" carousel.

Meesho lists every colour of a design as its own product. Each week:
  1. find groups: live, in-stock products from the same supplier and category whose titles match once colour
     words are removed (or whose IDs are consecutive uploads), in different colours
  2. Gemini (API) looks at one photo of each and confirms they're the same design, naming each colour
  3. build the carousel: a cover with every colour in a grid, then one slide per colour with its name
  4. Telegram preview with buttons; posting only after approval. "Link" comments get every colour's link.
State (groups posted / skipped, posted media ids for comment DMs): data/colour_posts.json.
"""
import difflib
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import DATA_DIR, IMAGES_DIR

log = logging.getLogger(__name__)

STATE_FILE = DATA_DIR / "colour_posts.json"
FEED = (1080, 1350)
MAX_COLOURS = 6                 # cover grid + 6 colour slides fits Instagram's 10
REPOST_DAYS = 60                # don't post the same design again within this many days
FONT = "C:/Windows/Fonts/segoeuib.ttf"
FONT_EMOJI = "C:/Windows/Fonts/seguiemj.ttf"
COLOUR_KEYS = ("Color", "Kurta Color", "Top Color", "Topwear Color", "Blouse Color")
COLOUR_WORDS = set("""black white red blue green yellow pink purple orange brown grey gray beige cream ivory
maroon navy sky teal turquoise mustard magenta peach coral olive lime mint lavender lilac wine rust gold golden
silver multi multicolor multicolour rani parrot bottle dark light baby off rose jambli violet aqua sea
firozi mehendi mehndi onion tomato cherry lemon""".split())


# --- state ----------------------------------------------------------------------------------------
def load_state() -> dict:
    s = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    s.setdefault("groups", {})       # group id -> {mids, colours, status, created, posted_at, ig_media_id, ...}
    return s


def save_state(s: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=1, ensure_ascii=False), encoding="utf-8")


def group_id(mids: list[str]) -> str:
    return "c-" + "-".join(sorted(mids))[:60]


# --- finding groups -------------------------------------------------------------------------------
def _product(mid: str) -> dict:
    p = DATA_DIR / mid / "product.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def main_colour(product: dict) -> str:
    a = product.get("attributes") or {}
    return next((str(a[k]).strip() for k in COLOUR_KEYS if a.get(k)), "")


def _norm_title(t: str) -> str:
    words = re.findall(r"[a-z]+", t.lower())
    return " ".join(w for w in words if w not in COLOUR_WORDS and len(w) > 1)


def _id_num(mid: str) -> int:
    try:
        return int(mid, 36)
    except ValueError:
        return -10**9


def candidates(rows: list[dict]) -> list[list[str]]:
    """Groups of 2+ live, in-stock products that look like one design in several colours (not yet checked
    by eye). Biggest groups first."""
    from .posts import styles
    from .workflow import Pipeline
    skip = [k.lower() for k in styles().get("skip_keywords") or []]
    live = []
    for r in rows:
        mid = str(r.get("meesho_id") or "")
        title = f"{r.get('shopify_title', '')} {r.get('meesho_title', '')}".lower()
        if (not mid or r.get("status") != "PUSHED" or str(r.get("shopify_status")).upper() != "ACTIVE"
                or str(r.get("in_stock")).upper() in ("FALSE", "NO", "0") or any(k in title for k in skip)):
            continue
        p = _product(mid)
        if not p or not Pipeline.clean_images(mid):
            continue
        live.append((mid, p))
    by_key: dict[tuple, list] = {}
    for mid, p in live:
        by_key.setdefault((p.get("supplier"), p.get("category")), []).append((mid, p))
    groups = []
    for items in by_key.values():
        if len(items) < 2:
            continue
        # union-find over "same design" links
        parent = {m: m for m, _ in items}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for i, (m1, p1) in enumerate(items):
            for m2, p2 in items[i + 1:]:
                pr1, pr2 = float(p1.get("price") or 0), float(p2.get("price") or 0)
                if pr1 and pr2 and abs(pr1 - pr2) / max(pr1, pr2) > 0.2:
                    continue
                t1, t2 = _norm_title(p1.get("title", "")), _norm_title(p2.get("title", ""))
                similar = t1 == t2 or difflib.SequenceMatcher(None, t1, t2).ratio() >= 0.85
                close_ids = abs(_id_num(m1) - _id_num(m2)) <= 40
                if similar or (close_ids and difflib.SequenceMatcher(None, t1, t2).ratio() >= 0.6):
                    parent[find(m1)] = find(m2)
        clusters: dict[str, list] = {}
        for m, p in items:
            clusters.setdefault(find(m), []).append((m, p))
        for c in clusters.values():
            # one product per colour (the seller's colour field; unknown colours kept for the eye check)
            seen, keep = set(), []
            for m, p in sorted(c, key=lambda x: -(x[1].get("rating_count") or 0)):
                col = main_colour(p).lower()
                if col and col in seen:
                    continue
                seen.add(col)
                keep.append(m)
            if len(keep) >= 2:
                groups.append(keep)
    return sorted(groups, key=len, reverse=True)


VERIFY_PROMPT = """These {n} photos are product listings from one seller (numbered 0 to {last} in order).
Which of them are the SAME garment design in DIFFERENT colours? Same design means: the same cut and
neckline, the same sleeves, the same border and the same print/embroidery motifs in the same places -- only
the colour differs (as a seller's colour options would). A similar style with a different neckline, border or
motif is NOT the same design. Ignore background, model and photo style. Give each image a short colour name a shopper would use
(e.g. "Parrot Green", "Wine", "Mustard"). Put an image in "same" only if its design matches the others and its
colour is different from every other image in "same"."""
VERIFY_SCHEMA = {"type": "object", "properties": {
    "same": {"type": "array", "items": {"type": "integer"}},
    "colours": {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"}, "colour": {"type": "string"}}, "required": ["index", "colour"]}}},
    "required": ["same", "colours"]}


def _thumb_bytes(path: Path, size=448) -> bytes:
    from io import BytesIO
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    im.thumbnail((size, size))
    buf = BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def verify(mids: list[str]) -> dict | None:
    """Gemini checks the photos: {"mids": [...], "colours": {mid: name}} for the matching ones, or None."""
    from google.genai import types
    from .ai import GeminiAI
    from .workflow import Pipeline
    mids = mids[:8]
    parts = [types.Part.from_bytes(data=_thumb_bytes(Pipeline.clean_images(m)[0]), mime_type="image/jpeg")
             for m in mids]
    g = GeminiAI()
    for model in g._text_models():
        try:
            resp = g._call(retries=1, model=model, contents=parts + [VERIFY_PROMPT.format(n=len(mids), last=len(mids) - 1)],
                           config=types.GenerateContentConfig(response_mime_type="application/json",
                                                              response_json_schema=VERIFY_SCHEMA, temperature=0.1))
            d = json.loads(resp.text)
            names = {c["index"]: c["colour"].strip().title() for c in d.get("colours", [])}
            same = [i for i in dict.fromkeys(d.get("same", [])) if 0 <= i < len(mids)]
            if len(same) < 2:
                return None
            return {"mids": [mids[i] for i in same], "colours": {mids[i]: names.get(i) or main_colour(_product(mids[i])) or "Colour"
                                                               for i in same}}
        except Exception as e:
            log.warning("colour check via %s failed: %s", model, str(e)[:150])
            if "RESOURCE_EXHAUSTED" in str(e):
                g._exhausted[model] = time.time() + (3 * 3600 if "PerDay" in str(e) else 120)
    return None


def find_group(rows: list[dict], exclude: set[str] = frozenset(), tries: int = 6) -> dict | None:
    """The next design to post: biggest candidate group not posted in REPOST_DAYS and not skipped,
    confirmed by Gemini. {"id", "mids", "colours"} or None."""
    s = load_state()
    since = (datetime.now() - timedelta(days=REPOST_DAYS)).isoformat()
    blocked = set(exclude)
    for gid, g in s["groups"].items():
        passed = g.get("status") == "PASSED" and g.get("created", "") >= (datetime.now() - timedelta(days=30)).isoformat()
        posted = g.get("status") == "POSTED" and g.get("posted_at", "") >= since
        if g.get("status") in ("SKIPPED", "NOT_SAME", "DRAFT") or passed or posted:
            blocked.update(g["mids"])
    checked = 0
    for mids in candidates(rows):
        if any(m in blocked for m in mids):
            continue
        res = verify(mids)
        checked += 1
        if res and len(res["mids"]) >= 2:
            ordered = sorted(res["mids"], key=lambda m: -(_product(m).get("rating_count") or 0))[:MAX_COLOURS]
            return {"id": group_id(ordered), "mids": ordered, "colours": {m: res["colours"][m] for m in ordered}}
        s["groups"][group_id(mids)] = {"mids": mids, "status": "NOT_SAME", "created": datetime.now().isoformat()}
        save_state(s)
        if checked >= tries:
            break
    return None


# --- images ---------------------------------------------------------------------------------------
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT, size)


def _best_photo(mid: str) -> Image.Image:
    """An AI model shot if this product has one (they look better), else its first original Meesho photo
    with the supplier code strip cut off (the listing copies have blurred padding baked in)."""
    from .posts import post_images
    from .workflow import Pipeline
    ai = post_images(mid) + Pipeline.ai_images(mid)
    if ai:
        return ImageOps.exif_transpose(Image.open(ai[0])).convert("RGB")
    orig = sorted((IMAGES_DIR / mid / "orig").glob("*"))
    if not orig:
        return ImageOps.exif_transpose(Image.open(Pipeline.clean_images(mid)[0])).convert("RGB")
    im = ImageOps.exif_transpose(Image.open(orig[0])).convert("RGB")
    return im.crop((0, 0, im.size[0], int(im.size[1] * 0.94)))


def _edge_colour(im: Image.Image) -> tuple[int, int, int]:
    """Median colour of the photo's outer edge: its own background, for filling around it."""
    import numpy as np
    a = np.asarray(im.resize((120, 150)))
    edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    return tuple(int(v) for v in np.median(edge, axis=0))


def _subject(im: Image.Image) -> tuple[float, float, float, float]:
    """Bounding box of the model/garment (fractions), found with rembg on a small copy; whole photo if it fails."""
    try:
        from rembg import remove
        from .studio import _rembg_session
        small = im.copy()
        small.thumbnail((512, 512))
        b = remove(small, session=_rembg_session(), post_process_mask=True).getchannel("A")             .point(lambda a: 255 if a > 40 else 0).getbbox()
        if b:
            return b[0] / small.size[0], b[1] / small.size[1], b[2] / small.size[0], b[3] / small.size[1]
    except Exception as e:
        log.warning("subject box failed: %s", e)
    return 0.0, 0.0, 1.0, 1.0


def _fit(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    """The garment filling `size`: a wide photo is cropped around the model (full height kept, a little margin);
    whatever still doesn't fill is padded with the photo's own edge colour (no blur)."""
    W, H = size
    w, h = im.size
    if w / h > W / H * 1.05:                       # wider than the slot: crop the sides around the model
        x0, _, x1, _ = _subject(im)
        need = max(h * W / H, (x1 - x0) * w * 1.15)   # slot shape, or wider if the model wouldn't fit
        cx = (x0 + x1) / 2 * w
        left = int(min(max(0, cx - need / 2), max(0, w - need)))
        im = im.crop((left, 0, int(min(w, left + need)), h))
        w, h = im.size
    if abs((h / w) / (H / W) - 1) < 0.06:
        return ImageOps.fit(im, size, Image.LANCZOS, centering=(0.5, 0.4))
    canvas = Image.new("RGB", size, _edge_colour(im))
    s = min(W / w, H / h)
    fg = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
    canvas.paste(fg, ((W - fg.size[0]) // 2, (H - fg.size[1]) // 2))
    return canvas


def _tile(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    """A grid tile completely filled: cropped around the model (a little of a wide skirt may be cut)."""
    x0, y0, x1, y1 = _subject(im)
    return ImageOps.fit(im, size, Image.LANCZOS, centering=((x0 + x1) / 2, max(0.0, min(1.0, y0 + 0.3 * (y1 - y0)))))


def _chip(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, size: int, anchor="lb") -> None:
    f = _font(size)
    box = draw.textbbox(xy, text, font=f, anchor=anchor)
    pad = size // 2
    draw.rounded_rectangle((box[0] - pad, box[1] - pad // 2, box[2] + pad, box[3] + pad // 2),
                           radius=size // 2, fill=(255, 255, 255, 230))
    draw.text(xy, text, font=f, fill=(30, 30, 30), anchor=anchor)


def cover_slide(photos: list[Image.Image], names: list[str], out: Path, headline: str) -> Path:
    """All colours in a grid with their names, and a headline band on top."""
    W, H = FEED
    band = 170
    n = len(photos)
    cols = 2 if n <= 4 else 3
    rows = (n + cols - 1) // cols
    cw, ch = W // cols, (H - band) // rows
    canvas = Image.new("RGB", FEED, (250, 246, 240))
    draw = ImageDraw.Draw(canvas, "RGBA")
    for i, (p, name) in enumerate(zip(photos, names)):
        r, c = divmod(i, cols)
        if r == rows - 1 and n % cols:            # centre an incomplete last row
            c += (cols - n % cols) / 2
        x, y = int(c * cw), band + r * ch
        canvas.paste(_tile(p, (cw - 8, ch - 8)), (x + 4, y + 4))
        _chip(draw, (x + 22, y + ch - 24), name, 30)
    draw.text((W // 2, band // 2 - 22), headline, font=_font(58), fill=(60, 30, 40), anchor="mm")
    draw.text((W // 2, band // 2 + 40), "Swipe to see every colour →", font=_font(30), fill=(120, 90, 100), anchor="mm")
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "JPEG", quality=94)
    return out


def colour_slide(photo: Image.Image, name: str, out: Path) -> Path:
    """The colour's photo filling 4:5 (whole garment kept) with its name."""
    canvas = _fit(photo, FEED)
    draw = ImageDraw.Draw(canvas, "RGBA")
    _chip(draw, (48, FEED[1] - 60), name, 44)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "JPEG", quality=94)
    return out


def build(group: dict) -> list[Path]:
    """Cover + one slide per colour, in data/colour_posts/<id>/."""
    out_dir = DATA_DIR / "colour_posts" / group["id"]
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    photos = [_best_photo(m) for m in group["mids"]]
    names = [group["colours"][m] for m in group["mids"]]
    files = [cover_slide(photos, names, out_dir / "00-cover.jpg", f"1 design · {len(photos)} colours")]
    files += [colour_slide(p, n, out_dir / f"{i:02d}-{re.sub(r'[^a-z0-9]+', '-', n.lower())}.jpg")
              for i, (p, n) in enumerate(zip(photos, names), 1)]
    return files


# --- caption, DM text -----------------------------------------------------------------------------
HOOKS = ["🎨 Same design, your favourite colour", "✨ Which colour are you picking?", "💗 One look, {n} moods",
         "🌈 Tell us your colour in the comments"]


def caption(group: dict, products) -> str:
    from .posts import _load, audience, garment, styles
    st = styles()["caption"]
    first = group["mids"][0]
    prow = products.get(first) or {}
    name = (_load(first, "copy").get("title") or garment(first) or prow.get("shopify_title", "")).strip()
    name = " ".join(w for w in name.split() if w.lower().strip(",") not in COLOUR_WORDS)   # covers every colour
    nums = [float(n) for m in group["mids"]
            for n in re.findall(r"\d+(?:\.\d+)?", str((products.get(m) or {}).get("my_price") or ""))]
    lo, hi = (int(min(nums)), int(max(nums))) if nums else (0, 0)
    price = "" if not nums else f"₹{lo}" if lo == hi else f"from ₹{lo}"
    colours = " · ".join(group["colours"][m] for m in group["mids"])
    aud = audience(first, prow)
    tags = " ".join(st["hashtags_by_audience"].get(aud) or [])
    hook = random.choice(HOOKS).format(n=len(group["mids"]))
    return (f"{hook}\n🎀 Comment \"Link\" and we'll DM you the link for every colour 🖇️🛍️\n"
            f"🛒 {name} · {price}\n🎨 {colours}\n.\n{tags}")


def links_text(group: dict, products) -> str:
    """One line per colour with its product link (store home if a colour isn't live)."""
    from .posts import product_link, store_url
    lines = []
    for m in group["mids"]:
        link = group.get("links", {}).get(m) or product_link(products.get(m) or {}) or store_url()
        lines.append(f"{group['colours'][m]}: {link}")
    return "\n".join(lines)


DM_TEMPLATE = "Hi {name} 💗 here are the links for every colour:\n{link}\nTap the one you love to order ✨"


# --- posting --------------------------------------------------------------------------------------
def publish(gid: str, products) -> dict:
    from . import facebook, instagram
    from .posts import product_link
    from .publisher import facebook_enabled
    from .shopify import Shopify
    s = load_state()
    g = s["groups"][gid]
    files = sorted((DATA_DIR / "colour_posts" / gid).glob("*.jpg"))
    if not files:
        raise RuntimeError("carousel images missing; make it again with /colours")
    g["links"] = {m: product_link(products.get(m) or {}) for m in g["mids"]}
    text = g.get("caption") or caption(g, products)
    from .publisher import export_ready, manual_mode
    if manual_mode():
        g.update(status="READY", caption=text)
        save_state(s)
        return {"manual": export_ready("c", gid, files, text)}
    shop = Shopify()
    urls = [shop.host_image(f, alt="same design in several colours") for f in files]
    res = instagram.publish_carousel(urls, text)
    g.update(status="POSTED", caption=text, ig_media_id=res["media_id"], ig_permalink=res["permalink"],
             posted_at=datetime.now().strftime("%Y-%m-%d %H:%M"))
    if facebook_enabled():
        try:
            fb = facebook.publish_photos(urls, text)
            g.update(fb_post_id=fb["post_id"], fb_permalink=fb["permalink"])
            res["fb_permalink"] = fb["permalink"]
        except Exception as e:
            log.exception("facebook colour post %s failed", gid)
            res["fb_error"] = str(e)
    save_state(s)
    return res


class CommentTargets:
    """Posted colour carousels as comment-DM targets (same shape check_comments uses for sheet rows)."""

    def __init__(self, products):
        self.products = products

    def recent(self, days: int = 7) -> list[dict]:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        out = []
        for gid, g in load_state()["groups"].items():
            if g.get("status") == "POSTED" and g.get("posted_at", "") >= cutoff:
                out.append({"gid": gid, "title": "this design", "price": "", "link": links_text(g, self.products),
                            "dm_template": DM_TEMPLATE, "ig_media_id": g.get("ig_media_id", ""),
                            "fb_post_id": g.get("fb_post_id", ""), "dms_sent": g.get("dms_sent", 0)})
        return out

    def upsert(self, gid: str, **fields) -> None:
        s = load_state()
        s["groups"].get(gid, {}).update(fields)
        save_state(s)
