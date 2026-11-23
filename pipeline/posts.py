"""Instagram posts: one carousel per product, AI images in real-looking scenes with the face hidden.

Styles/prompts live in post_styles.yaml (re-read on every run). Post state lives in the
"Posts" tab of the Google Sheet, worked through top to bottom.
"""
import json
import logging
import random
import shutil
import time
from pathlib import Path

import gspread
import yaml
from gspread.utils import rowcol_to_a1
from PIL import Image, ImageDraw, ImageFont

from .config import DATA_DIR, IMAGES_DIR, ROOT, env
from .sheet import now

log = logging.getLogger(__name__)

STYLES_FILE = ROOT / "post_styles.yaml"
SPACE = "LPX55/Qwen-Image-Edit-2511-Turbo-Lightning"   # Qwen-Image-Edit-2511 + Lightning, ~15 s/image

POST_COLUMNS = [
    "meesho_id", "post_status", "title", "audience", "price", "link", "images", "shots", "caption",
    "scheduled_for", "ig_media_id", "ig_permalink", "posted_at", "dms_sent", "error", "updated_at",
    "fb_post_id", "fb_permalink",   # added later: kept at the end so older sheets only gain columns
    "source",                       # "manual" = images sent via Telegram (/originals); else Kaggle
]
POST_STATUSES = ["QUEUED", "GENERATING", "GENERATED", "APPROVED", "READY", "POSTED", "SKIP", "ERROR"]


def styles() -> dict:
    with open(STYLES_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


# --- product facts ------------------------------------------------------------------------------
def _load(mid: str, name: str) -> dict:
    p = DATA_DIR / mid / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def audience(mid: str, row: dict | None = None) -> str:
    product, row = _load(mid, "product"), row or {}
    text = " ".join([str(row.get("collections", "")), str(product.get("category", "")),
                     str(row.get("category", ""))]).lower()
    sizes = " ".join(v.get("name", "") for v in product.get("variants", [])).lower()
    if "jewel" in text or "earring" in text or "necklace" in text:
        return "jewellery"
    if "kid" in text or "girl" in text or "year" in sizes or "yrs" in sizes:
        return "kids"
    return "women"


def garment(mid: str) -> str:
    """Short garment description for the prompt, e.g. 'Black Cotton Linen Bandhani Print Lehenga Choli'."""
    copy, product = _load(mid, "copy"), _load(mid, "product")
    title = copy.get("title") or product.get("title", "")
    for w in ("Girls ", "Girl's ", "Women's ", "Womens ", "Women ", "Kids "):
        title = title.replace(w, "")
    return title.strip()


def reference_image(mid: str, out_dir: Path, crop_bottom=0.06, size=1024) -> Path:
    """First Meesho photo, supplier code cropped off, downsized for upload."""
    src = sorted((IMAGES_DIR / mid / "orig").glob("*"))[0]
    im = Image.open(src).convert("RGB")
    w, h = im.size
    im = im.crop((0, 0, w, int(h * (1 - crop_bottom))))
    im.thumbnail((size, size))
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "reference.jpg"
    im.save(p, quality=92)
    return p


# --- planning -----------------------------------------------------------------------------------
def plan(mid: str, aud: str, recent_scenes: list[str], st: dict | None = None,
         rng: random.Random | None = None) -> list[dict]:
    """Pick scene/pose/face/model for each shot: no scene twice in a post, avoid recent scenes."""
    st, rng = st or styles(), rng or random.Random()
    a = st["audiences"][aud]
    pool = list(a.get("scenes_only") or st["scenes"])
    fresh = [s for s in pool if s not in recent_scenes[-st.get("avoid_recent_scenes", 0):]]
    scenes = rng.sample(fresh if len(fresh) >= len(st["shots"]) else pool, len(st["shots"]))
    model = rng.choice(a["model"])          # same model description across the carousel
    faces = rng.sample(a["face_hiding"], min(len(a["face_hiding"]), len(st["shots"])))
    shots = []
    for i, (shot, scene) in enumerate(zip(st["shots"], scenes)):
        sc = st["scenes"][scene]
        framing = a.get("framing_override") or shot["framing"]
        cap = lambda t: t[:1].upper() + t[1:]  # noqa: E731
        face = faces[i % len(faces)]
        # some face-hiding lines already fix the pose (back view, walking away, twirling)
        pose = ("natural and relaxed" if any(w in face for w in ("back view", "walks", "twirls"))
                else rng.choice(a["poses"]))
        values = dict(model=model, garment=garment(mid), pose=pose, face=cap(face), scene=sc["text"], light=rng.choice(sc["light"]),
                      framing=cap(framing), lock=st["garment_lock"].strip())
        prompt = " ".join(st["prompt"].format(**values).split())
        shots.append(dict(name=shot["name"], scene=scene, pose=values["pose"], face=values["face"],
                          prompt=prompt, seed=rng.randint(1, 2**31 - 1)))
    return shots


# --- generation ---------------------------------------------------------------------------------
class QuotaExceeded(RuntimeError):
    pass


def generate_image(reference: Path, prompt: str, seed: int, out: Path, size=(832, 1104), steps=8) -> Path:
    from gradio_client import Client, handle_file
    client = Client(SPACE, token=env("HF_TOKEN"), verbose=False)
    try:
        result, _ = client.predict(
            images=[{"image": handle_file(str(reference))}], prompt=prompt, seed=seed,
            randomize_seed=False, true_guidance_scale=1.0, num_inference_steps=steps,
            height=size[1], width=size[0], rewrite_prompt=False, num_images_per_prompt=1, api_name="/infer")
    except Exception as e:
        if "quota" in str(e).lower():
            raise QuotaExceeded(str(e)) from e
        raise
    item = result[0]
    src = item["image"] if isinstance(item.get("image"), str) else item["image"]["path"]
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.open(src).convert("RGB").save(out, quality=95)
    return out


def watermark(path: Path, st: dict, rng: random.Random | None = None) -> None:
    wm, rng = st.get("watermark") or {}, rng or random.Random()
    if not wm.get("text"):
        return
    im = Image.open(path).convert("RGBA")
    w, h = im.size
    try:
        font = ImageFont.truetype("arial.ttf", max(12, int(h * wm.get("size", 0.022))))
    except OSError:
        font = ImageFont.load_default()
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    x0, y0, x1, y1 = wm.get("region", [0.08, 0.55, 0.65, 0.9])
    pos = (int(w * rng.uniform(x0, x1)), int(h * rng.uniform(y0, y1)))
    ImageDraw.Draw(layer).text(pos, wm["text"], font=font, fill=(255, 255, 255, int(255 * wm.get("opacity", 0.45))))
    Image.alpha_composite(im, layer).convert("RGB").save(path, quality=95)


def caption(mid: str, aud: str, price, link: str, st: dict | None = None, rng=None) -> str:
    st, rng = st or styles(), rng or random.Random()
    c = st["caption"]
    copy = _load(mid, "copy")
    tags = copy.get("tags") or []
    keywords = " · ".join(dict.fromkeys([t.lower() for t in tags] + ["affordable fashion", "the noor naksh"]))
    text = c["template"].format(hook=rng.choice(c["hooks"]), title=copy.get("title", garment(mid)),
                                price=price, mrp="", link=link or "(link coming soon)",
                                hashtags=" ".join(c["hashtags_by_audience"][aud]), keywords=keywords)
    if c.get("ai_note"):
        text += f"\n.\n{c['ai_note']}"
    return text


def product_link(row: dict) -> str:
    """Public product URL; only exists once the product is Active on the Online Store."""
    if not row.get("shopify_product_id"):
        return ""
    from .shopify import Shopify
    info = Shopify().product_info(row["shopify_product_id"]) or {}
    return info.get("onlineStoreUrl") or ""


_store_url = {"url": ""}


def store_url() -> str:
    """Store home page, used as the link for posts whose product isn't Active yet."""
    url = (styles().get("no_store_link") or {}).get("store_url") or _store_url["url"]
    if not url:
        from .shopify import Shopify
        url = _store_url["url"] = Shopify().gql("{ shop { primaryDomain { url } } }")["shop"]["primaryDomain"]["url"]
    return url


def post_without_link() -> bool:
    return bool((styles().get("no_store_link") or {}).get("post_anyway", True))


def post_dir(mid: str) -> Path:
    return IMAGES_DIR / mid / "post"


def post_images(mid: str) -> list[Path]:
    return sorted(post_dir(mid).glob("[0-9]*.jpg"))


def make_post(mid: str, aud: str, recent_scenes: list[str]) -> list[dict]:
    """Generate the carousel for one product. Returns the shot plan (with files)."""
    st = styles()
    out = post_dir(mid)
    if out.exists():
        shutil.rmtree(out)
    ref = reference_image(mid, out)
    shots = plan(mid, aud, recent_scenes, st)
    size = tuple(st.get("image_size", [832, 1104]))
    for i, shot in enumerate(shots, 1):
        t = time.time()
        f = generate_image(ref, shot["prompt"], shot["seed"], out / f"{i:02d}-{shot['name']}.jpg", size)
        watermark(f, st)
        shot["file"] = f.name
        log.info("post %s shot %d/%d %s @ %s (%.0fs)", mid, i, len(shots), shot["name"], shot["scene"],
                 time.time() - t)
    (out / "plan.json").write_text(json.dumps(shots, indent=1, ensure_ascii=False), encoding="utf-8")
    return shots


# --- sheet --------------------------------------------------------------------------------------
class PostSheet:
    """The "Posts" tab: one row per product, posted top to bottom."""

    def __init__(self, book: gspread.Spreadsheet | None = None):
        if book is None:
            gc = gspread.service_account(filename=env("GOOGLE_SERVICE_ACCOUNT_FILE"))
            book = gc.open_by_key(env("GOOGLE_SHEET_ID"))
        try:
            self.ws = book.worksheet("Posts")
            header = self.ws.row_values(1)
            if header and header != POST_COLUMNS and header == POST_COLUMNS[:len(header)]:
                if self.ws.col_count < len(POST_COLUMNS):
                    self.ws.add_cols(len(POST_COLUMNS) - self.ws.col_count)
                self.ws.update(range_name="A1", values=[POST_COLUMNS])
        except gspread.WorksheetNotFound:
            self.ws = book.add_worksheet("Posts", rows=1000, cols=len(POST_COLUMNS))
            self.ws.update(range_name="A1", values=[POST_COLUMNS])
            self.ws.freeze(rows=1, cols=2)
            col = POST_COLUMNS.index("post_status")
            book.batch_update({"requests": [
                {"repeatCell": {"range": {"sheetId": self.ws.id, "startRowIndex": 0, "endRowIndex": 1},
                                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                                "fields": "userEnteredFormat.textFormat.bold"}},
                {"setDataValidation": {
                    "range": {"sheetId": self.ws.id, "startRowIndex": 1, "startColumnIndex": col,
                              "endColumnIndex": col + 1},
                    "rule": {"condition": {"type": "ONE_OF_LIST",
                                           "values": [{"userEnteredValue": s} for s in POST_STATUSES]},
                             "showCustomUi": True, "strict": True}}},
            ]})

    def all(self) -> list[dict]:
        return self.ws.get_all_records(expected_headers=POST_COLUMNS)

    def _row_number(self, mid: str) -> int | None:
        ids = self.ws.col_values(1)
        return ids.index(mid) + 1 if mid in ids else None

    def upsert(self, mid: str, **fields) -> None:
        unknown = set(fields) - set(POST_COLUMNS)
        if unknown:
            raise KeyError(f"Unknown Posts columns: {unknown}")
        fields["updated_at"] = now()
        r = self._row_number(mid)
        if r is None:
            row = {c: "" for c in POST_COLUMNS}
            row.update(meesho_id=mid, **fields)
            self.ws.append_row([row[c] for c in POST_COLUMNS], value_input_option="RAW", table_range="A1")
            return
        self.ws.batch_update([{"range": rowcol_to_a1(r, POST_COLUMNS.index(k) + 1), "values": [[v]]}
                              for k, v in fields.items()], value_input_option="RAW")

    def append(self, rows: list[dict]) -> None:
        """Add many rows in one request (Sheets allows ~60 writes/minute)."""
        if rows:
            self.ws.append_rows([[r.get(c, "") if c != "updated_at" else now() for c in POST_COLUMNS]
                                 for r in rows], value_input_option="RAW", table_range="A1")

    def next(self, status: str, n: int) -> list[dict]:
        return [r for r in self.all() if r["post_status"] == status][:n]

    def recent_scenes(self, limit=30) -> list[str]:
        """Scenes used by the most recently generated posts (oldest first)."""
        out = []
        for p in sorted(post_dir_glob(), key=lambda p: p.stat().st_mtime)[-limit:]:
            try:
                out += [s["scene"] for s in json.loads(p.read_text(encoding="utf-8"))]
            except (OSError, ValueError, KeyError):
                pass
        return out


def post_dir_glob():
    return IMAGES_DIR.glob("*/post/plan.json")
