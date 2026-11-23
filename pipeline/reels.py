"""Reels made on the laptop (CPU only): product photos -> 9:16 video cut to the beat of a music track,
with zooms/pans, transitions, hook/price/CTA text, encoded with FFmpeg.

Templates live in reel_templates.yaml (re-read every render). Music: put royalty-free tracks
(mp3/m4a/wav) in music/; without music the reel uses a 120 BPM grid and a silent track.
Each frame is drawn with PIL and piped to ffmpeg, so a 15 s reel takes ~1-3 min on a slow laptop.
"""
import json
import logging
import random
import subprocess
import time
from pathlib import Path

import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .config import DATA_DIR, IMAGES_DIR, ROOT

log = logging.getLogger(__name__)

TEMPLATES_FILE = ROOT / "reel_templates.yaml"
MUSIC_DIR = ROOT / "music"
STATE_FILE = DATA_DIR / "reels.json"
W, H, FPS = 1080, 1920, 30
PAD = 1.18            # canvas is this much bigger than the frame, so zooms/pans have room
FONT_BOLD = "C:/Windows/Fonts/segoeuib.ttf"
FONT_EMOJI = "C:/Windows/Fonts/seguiemj.ttf"
AUDIO_EXT = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac")


def templates() -> dict:
    """reel_templates.yaml plus the styles copied from viral reels (reel_templates_copied.yaml)."""
    with open(TEMPLATES_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    from .viral import copied
    cfg["templates"].update(copied())
    return cfg


def reels_dir(mid: str) -> Path:
    return IMAGES_DIR / mid / "reels"


# --- product assets -------------------------------------------------------------------------------
def product_assets(mid: str) -> dict[str, list[Path]]:
    """real = cleaned Meesho photos; ai = carousel/AI shots (Kaggle, Gemini app, listing AI images)."""
    from .posts import post_images
    from .workflow import Pipeline
    real = [p for p in Pipeline.clean_images(mid) if p.exists()][:6]
    ai = list(dict.fromkeys(post_images(mid) + Pipeline.ai_images(mid)))
    if not real and not ai:
        raise RuntimeError(f"no photos saved for {mid}")
    return {"real": real, "ai": ai}


# what the product is, in one or two words for hooks ("guess the price of this lehenga"); first match wins
KINDS = [("lehenga", "lehenga"), ("saree", "saree"), ("sari ", "saree"), ("anarkali", "anarkali"),
         ("sharara", "sharara set"), ("kaftan", "kaftan set"), ("kurta set", "kurta set"), ("kurti", "kurti"),
         ("kurta", "kurta"), ("salwar", "suit"), ("co-ord", "co-ord set"), ("nightdress", "nightdress"),
         ("gown", "gown"), ("dress", "dress"), ("mangalsutra", "mangalsutra"), ("choker", "choker set"),
         ("necklace", "necklace set"), ("jhumka", "jhumkas"), ("earring", "earrings"), ("chandbali", "earrings"),
         ("bangle", "bangles"), ("bracelet", "bracelet"), ("anklet", "anklet"), ("ring", "ring"),
         ("jewellery", "jewellery set")]


def facts(mid: str, prow: dict) -> dict:
    """title = full name; short = what it is ("lehenga"); name = a short on-screen name ("Green Lehenga")."""
    from .posts import garment
    title = str(prow.get("shopify_title") or garment(mid) or mid)
    low = (garment(mid) or title).lower() + " "
    kind = next((k for w, k in KINDS if w in low), "look")
    name = garment(mid) or title
    if len(name) > 26:
        first = name.split()[0]
        name = (first + " " + kind).title() if first.lower() not in kind else kind.title()
    price = str(prow.get("my_price") or "").replace(".0", "")
    return {"title": title, "short": kind, "name": name, "price": price}


# --- images ---------------------------------------------------------------------------------------
def make_canvas(src: Path, mid: str | None = None) -> Image.Image:
    """The photo filling a 9:16 canvas PAD x frame size, cropped around the model (see photo_rect).
    Only photos too wide to crop without cutting the model show a blurred copy at the edges."""
    cw, ch = int(W * PAD), int(H * PAD)
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    x0, y0, fw, fh = photo_rect(src, mid)
    fg = im.resize((fw, fh), Image.LANCZOS)
    if fw >= cw and fh >= ch:
        bg = Image.new("RGB", (cw, ch))
    else:
        bg = ImageOps.fit(im, (cw // 4, ch // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(12))
        bg = Image.blend(bg, Image.new("RGB", bg.size, (0, 0, 0)), 0.3).resize((cw, ch), Image.BILINEAR)
    bg.paste(fg, (x0, y0))
    return bg


def blur_canvas(src: Path) -> Image.Image:
    """Background for parallax slots: the photo blown up, heavily blurred and darkened."""
    cw, ch = int(W * PAD), int(H * PAD)
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    bg = ImageOps.fit(im, (cw // 8, ch // 8), Image.BILINEAR).filter(ImageFilter.GaussianBlur(6))
    return Image.blend(bg, Image.new("RGB", bg.size, (0, 0, 0)), 0.35).resize((cw, ch), Image.BILINEAR)


def cutout(src: Path, cache: Path) -> Image.Image | None:
    """Product cut out of its background (rembg, CPU, cached). None if it fails."""
    if cache.exists():
        return Image.open(cache).convert("RGBA")
    try:
        from rembg import remove
        from .studio import _rembg_session
        im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
        cut = remove(im, session=_rembg_session(), post_process_mask=True)
        box = cut.getchannel("A").point(lambda a: 255 if a > 40 else 0).getbbox()
        if not box:
            return None
        cut = cut.crop(box)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cut.save(cache)
        return cut
    except Exception as e:
        log.warning("cutout failed for %s: %s", src, e)
        return None


def ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def camera(effect: str, t: float, rng_dir: int) -> tuple[float, float, float]:
    """(zoom, cx, cy) for progress t in [0,1]. zoom 1 = whole canvas; centre in canvas fractions."""
    e = ease(t)
    if effect == "zoom_in":
        return 1.0 + 0.14 * e, 0.5, 0.5
    if effect == "zoom_out":
        return 1.14 - 0.14 * e, 0.5, 0.5
    if effect == "punch":   # snaps in on the beat, settles quickly
        return 1.0 + 0.10 * max(0.0, 1 - t / 0.35) ** 2 + 0.04 * e, 0.5, 0.5
    if effect == "pan_down":
        return 1.16, 0.5, 0.42 + 0.16 * e
    if effect == "pan_up":
        return 1.16, 0.5, 0.58 - 0.16 * e
    if effect == "drift":
        return 1.08, 0.5 + rng_dir * (0.05 * e - 0.025), 0.5
    if effect == "detail":  # close on the fabric/embroidery, creeping in
        return 1.55 + 0.10 * e, 0.5, 0.45
    return 1.02, 0.5, 0.5   # still


def canvas_box(size: tuple[int, int], zoom: float, cx: float, cy: float) -> tuple[float, float, float, float]:
    """The part of the canvas (x0, y0, w, h) that fills the frame."""
    cw, ch = size
    zoom = max(zoom, 1.0)
    bw, bh = cw / zoom, ch / zoom   # zoom 1 = the whole canvas (photo fully visible)
    x0 = max(0.0, min(cx * cw - bw / 2, cw - bw))
    y0 = max(0.0, min(cy * ch - bh / 2, ch - bh))
    return x0, y0, bw, bh


def frame_from_canvas(canvas: Image.Image, zoom: float, cx: float, cy: float) -> Image.Image:
    x0, y0, bw, bh = canvas_box(canvas.size, zoom, cx, cy)
    return canvas.resize((W, H), Image.BILINEAR, box=(x0, y0, x0 + bw, y0 + bh))


# --- keeping text off the model ------------------------------------------------------------------
SAFE_TOP, SAFE_BOTTOM = 0.075, 0.80   # text stays between these (Instagram's header / caption cover the rest)
FACE = 0.2                            # top fraction of the model treated as head/face: text never covers it
GAP = 18
CTA_SECONDS = 2.4                     # the closing price + CTA appear only this long before the end


def photo_rect(src: Path, mid: str | None = None) -> tuple[int, int, int, int]:
    """Where make_canvas puts the photo: (x0, y0, w, h) in canvas pixels. The photo fills the canvas
    (cropped, centred on the model, head kept near the top); if that would cut the model's sides it is
    scaled down only as far as needed (the gaps then show a blurred copy). A wide skirt may be trimmed a little."""
    cw, ch = int(W * PAD), int(H * PAD)
    with Image.open(src) as im:
        w, h = ImageOps.exif_transpose(im).size
    bx0, by0, bx1, by1 = subject_box(src, mid) if mid else (0.0, 0.0, 1.0, 1.0)
    s_fit, s_cover = min(cw / w, ch / h), max(cw / w, ch / h)
    s = max(s_fit, min(s_cover, cw * 1.15 / max(1.0, (bx1 - bx0) * w)))   # may trim a flared skirt a little
    fw, fh = round(w * s), round(h * s)
    x0 = round(cw / 2 - (bx0 + bx1) / 2 * fw)
    y0 = round(ch * 0.06 - by0 * fh)
    x0 = min(0, max(cw - fw, x0)) if fw >= cw else (cw - fw) // 2
    y0 = min(0, max(ch - fh, y0)) if fh >= ch else (ch - fh) // 2
    return x0, y0, fw, fh


def subject_box(src: Path, mid: str) -> tuple[float, float, float, float]:
    """Model/product bounding box in the photo (fractions x0, y0, x1, y1), found with rembg and cached.
    Also saves the cut-out used by parallax slots. Whole photo if it fails."""
    cache = reels_dir(mid) / "subjects.json"
    key = f"{src.parent.name}/{src.name}"
    data = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    if key in data:
        return tuple(data[key])
    box = (0.0, 0.0, 1.0, 1.0)
    try:
        from rembg import remove
        from .studio import _rembg_session
        im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
        small = im.copy()
        small.thumbnail((640, 640))
        cut = remove(small, session=_rembg_session(), post_process_mask=True)
        b = cut.getchannel("A").point(lambda a: 255 if a > 40 else 0).getbbox()
        if b:
            sw, sh = small.size
            box = (b[0] / sw, b[1] / sh, b[2] / sw, b[3] / sh)
    except Exception as e:
        log.warning("subject box for %s failed: %s", src, e)
    data[key] = [round(v, 4) for v in box]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return box


def subject_span(s: dict, canvas: Image.Image, cut: Image.Image | None, mid: str) -> tuple[float, float]:
    """Top and bottom of the model in the frame (pixels) over the whole slot."""
    tops, bottoms = [], []
    if s["effect"] == "parallax" and cut is not None:
        w, h = cut.size
        for t in (0.0, 1.0):
            sc = min(W * 0.86 / w, H * 0.80 / h) * (1.0 + 0.08 * ease(t))
            y = (H - h * sc) // 2 + 30
            tops.append(y)
            bottoms.append(y + h * sc)
        return min(tops), max(bottoms)
    px, py, pw, ph = photo_rect(Path(s["image"]), mid)
    bx0, by0, bx1, by1 = subject_box(Path(s["image"]), mid)
    sy0, sy1 = py + by0 * ph, py + by1 * ph
    for t in (0.0, 0.5, 1.0):
        _, y0, _, bh = canvas_box(canvas.size, *camera(s["effect"], t, s["dir"]))
        tops.append((sy0 - y0) * H / bh)
        bottoms.append((sy1 - y0) * H / bh)
    return max(0.0, min(tops)), min(float(H), max(bottoms))


def face_zone(s: dict, canvas, cut, mid: str) -> tuple[float, float]:
    """Top and bottom (pixels) of the model's head/face over the whole slot."""
    sy0, sy1 = subject_span(s, canvas, cut, mid)
    return sy0, sy0 + FACE * (sy1 - sy0) + GAP


def parallax_frame(bg: Image.Image, cut: Image.Image, t: float, rng_dir: int) -> Image.Image:
    """2.5D look: blurred background drifts one way, the cut-out product the other way and grows."""
    e = ease(t)
    frame = frame_from_canvas(bg, 1.06 + 0.04 * e, 0.5 - rng_dir * (0.03 * e - 0.015), 0.5)
    w, h = cut.size
    s = min(W * 0.86 / w, H * 0.80 / h) * (1.0 + 0.08 * e)
    fg = cut.resize((max(1, round(w * s)), max(1, round(h * s))), Image.BILINEAR)
    x = (W - fg.size[0]) // 2 + round(rng_dir * (40 * e - 20))
    y = (H - fg.size[1]) // 2 + 30
    frame.paste(fg, (x, y), fg)
    return frame


# --- text -----------------------------------------------------------------------------------------
def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF or o in (0xFE0F, 0x200D)


def _runs(text: str):
    run, emoji = "", None
    for ch in text:
        e = _is_emoji(ch)
        if run and e != emoji:
            yield run, emoji
            run = ""
        run, emoji = run + ch, e
    if run:
        yield run, emoji


def text_image(text: str, size: int, color=(255, 255, 255), box=(0, 0, 0, 150), pad=(34, 18),
               max_width=W - 140, radius=28) -> Image.Image:
    """Multi-line text (emoji in colour) on a rounded box, as an RGBA image."""
    font, efont = ImageFont.truetype(FONT_BOLD, size), ImageFont.truetype(FONT_EMOJI, int(size * 0.9))
    draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))

    def width(line):
        return sum(draw.textlength(r, font=efont if e else font) for r, e in _runs(line))

    lines = []
    for para in str(text).split("\n"):
        line = ""
        for word in para.split(" "):
            test = f"{line} {word}".strip()
            if line and width(test) > max_width:
                lines.append(line)
                line = word
            else:
                line = test
        lines.append(line)
    lh = int(size * 1.28)
    tw = int(max(width(line) for line in lines))
    im = Image.new("RGBA", (tw + 2 * pad[0], lh * len(lines) + 2 * pad[1]), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if box:
        d.rounded_rectangle((0, 0, im.size[0] - 1, im.size[1] - 1), radius=radius, fill=box)
    for i, line in enumerate(lines):
        x = pad[0] + (tw - width(line)) / 2
        y = pad[1] + i * lh
        for r, e in _runs(line):
            if e:
                d.text((x, y + size * 0.08), r, font=efont, embedded_color=True)
            else:
                if not box:   # no box: dark outline so it reads on any photo
                    d.text((x, y), r, font=font, fill=color, stroke_width=max(2, size // 14), stroke_fill=(0, 0, 0, 200))
                else:
                    d.text((x, y), r, font=font, fill=color)
            x += draw.textlength(r, font=efont if e else font)
    return im


class Overlay:
    """A text image shown from start to end (seconds), popping in over 0.2 s."""

    def __init__(self, im: Image.Image, start: float, end: float, y: float, pop=True, group: str | None = None):
        self.im, self.start, self.end, self.y, self.pop, self.group = im, start, end, y, pop, group

    @property
    def top(self) -> float:
        return H * self.y - self.im.size[1] / 2

    def draw(self, frame: Image.Image, t: float) -> None:
        if not self.start <= t < self.end:
            return
        k = min(1.0, (t - self.start) / 0.2) if self.pop else 1.0
        im = self.im
        if k < 1:
            s = 0.75 + 0.25 * ease(k)
            im = im.resize((max(1, int(im.size[0] * s)), max(1, int(im.size[1] * s))), Image.BILINEAR)
            a = im.getchannel("A").point(lambda v: int(v * k))
            im.putalpha(a)
        x = (W - im.size[0]) // 2
        y = int(H * self.y - im.size[1] / 2)
        frame.paste(im, (x, y), im)


# --- music ----------------------------------------------------------------------------------------
def pick_track(rng: random.Random, name: str | None = None) -> Path | None:
    tracks = sorted(p for p in MUSIC_DIR.glob("*") if p.suffix.lower() in AUDIO_EXT) if MUSIC_DIR.exists() else []
    if name:
        tracks = [p for p in tracks if name.lower() in p.name.lower()] or tracks
    if not name and len(tracks) > 3:     # don't repeat the tracks of the last few reels
        recent = [r.get("track") for r in sorted(load_state().values(), key=lambda r: r.get("created", ""))[-8:]]
        tracks = [p for p in tracks if p.stem not in recent] or tracks
    return rng.choice(tracks) if tracks else None


def beat_grid(track: Path | None, needed_s: float) -> tuple[list[float], float]:
    """Beat times (seconds, relative to the reel start) and where to start in the track. The start is
    the beat that begins the loudest stretch of `needed_s` seconds in the first 2 minutes."""
    if not track:
        return [i * 0.5 for i in range(int(needed_s / 0.5) + 64)], 0.0
    import librosa
    cache = MUSIC_DIR / ".beats" / f"{track.stem}.json"
    if cache.exists() and cache.stat().st_mtime >= track.stat().st_mtime:
        data = json.loads(cache.read_text(encoding="utf-8"))
    else:
        y, sr = librosa.load(str(track), sr=22050, mono=True, duration=150)
        _, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
        rms = librosa.feature.rms(y=y)[0]
        data = {"beats": [float(b) for b in beats], "rms": [float(v) for v in rms],
                "rms_hop": 512 / sr, "duration": len(y) / sr}
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
    beats, rms, hop = data["beats"], np.array(data["rms"]), data["rms_hop"]
    if len(beats) < 8:
        return [i * 0.5 for i in range(int(needed_s / 0.5) + 64)], 0.0
    best, best_i = -1.0, 0
    for i, b in enumerate(beats):
        if b + needed_s > data["duration"]:
            break
        seg = rms[int(b / hop):int((b + needed_s) / hop)]
        if len(seg) and seg.mean() > best:
            best, best_i = float(seg.mean()), i
    start = beats[best_i]
    rel = [b - start for b in beats[best_i:]]
    gap = float(np.median(np.diff(beats)))
    while rel[-1] < needed_s + 8:      # extend with the tempo if the track ends early
        rel.append(rel[-1] + gap)
    return rel, start


# --- render ---------------------------------------------------------------------------------------
class _Words(dict):
    def __missing__(self, key):     # unknown {placeholder} -> empty
        return ""


def plan_reel(mid: str, prow: dict, template: str, seed: int) -> dict:
    """Pick photos, text and timing for one reel (no rendering)."""
    cfg = templates()
    tpl = cfg["templates"][template]
    rng = random.Random(seed)
    assets = product_assets(mid)
    # AI model shots look far better than supplier photos, so they lead: the hero (opening shot) is the
    # first AI shot when there is one, "any" slots take AI shots first. No photo is shown twice; slots
    # left without a photo are dropped (the CTA slot then reuses the hero).
    pools = {"ai": assets["ai"][:1] + rng.sample(assets["ai"][1:], len(assets["ai"]) - 1),
             "real": assets["real"][:1] + rng.sample(assets["real"][1:], len(assets["real"]) - 1)}
    hero = (pools["ai"] or pools["real"])[0]
    used: set[Path] = set()
    counters = {"any": 0}

    def take(source: str) -> Path | None:
        if source == "hero":
            order = ["hero", "ai", "real"]
        elif source == "any":
            order = ["ai", "real"] if counters["any"] % 3 != 2 or not pools["real"] else ["real", "ai"]
            counters["any"] += 1
        else:
            order = [source, "real" if source == "ai" else "ai"]
        for src in order:
            for p in ([hero] if src == "hero" else pools[src]):
                if p not in used:
                    used.add(p)
                    return p
        return None

    raw = [s for s in tpl["slots"] for _ in range(int(s.get("repeat", 1)))]
    slots = []
    for i, s in enumerate(raw):
        img = take(s.get("source", "any"))
        if img is None:
            if i < len(raw) - 1:
                continue
            img = hero
        slots.append({"n": i, "image": str(img),
                      "beats": float(s.get("beats", 2)), "seconds": s.get("seconds"),
                      "effect": s.get("effect", "zoom_in"), "transition": s.get("transition", "cut"),
                      "text": s.get("text"), "dir": rng.choice([-1, 1])})
    f = facts(mid, prow)
    words = {**f, "brand": cfg["defaults"].get("brand", "")}

    def fill(text: str) -> str:
        try:
            return str(text).format_map(_Words(words))
        except (ValueError, IndexError):   # stray braces in a hand-edited line
            return str(text)

    texts = {"hook": fill(rng.choice(tpl.get("hooks") or cfg["defaults"]["hooks"])),
             "price": fill(tpl.get("price_text") or cfg["defaults"]["price_text"]) if f["price"] else "",
             "cta": fill(tpl.get("cta") or cfg["defaults"]["cta"]),
             "title": f["name"]}
    # copied styles: text placed by shot range; anything that isn't a text kind is a literal line
    overlays = [{**o, "text": o["text"] if o["text"] in texts else fill(o["text"])} for o in tpl.get("overlays") or []]
    track = pick_track(rng, tpl.get("music"))
    return {"mid": mid, "template": template, "seed": seed, "slots": slots, "texts": texts, "overlays": overlays,
            "timing": tpl.get("timing", "beats"), "text_style": tpl.get("text_style", "box"),
            "track": str(track) if track else "",
            "max_seconds": float(tpl.get("max_seconds") or cfg["defaults"].get("max_seconds", 20)),
            "brand": cfg["defaults"].get("brand", "")}


def render(plan: dict, out: Path) -> dict:
    """Render a planned reel to an mp4. Returns {file, seconds, track, thumb_ms}."""
    t0 = time.time()
    track = Path(plan["track"]) if plan["track"] else None
    by_seconds = plan.get("timing") == "seconds"
    if by_seconds:      # copied reel whose cuts didn't follow its music: keep its own timing
        need = sum(float(s["seconds"] or 1) for s in plan["slots"])
    else:
        need = sum(s["beats"] for s in plan["slots"]) * 0.7
    beats, start = beat_grid(track, needed_s=min(plan["max_seconds"], need) + 2)
    halves = []         # beat grid with half-beats, so slots can be 0.5, 1, 1.5 ... beats long
    for a, b in zip(beats, beats[1:]):
        halves += [a, (a + b) / 2]
    halves.append(beats[-1])
    # slot boundaries; stop adding slots once max_seconds is reached (keep the last = CTA)
    slots, i, t = [], 0, 0.0
    for n, s in enumerate(plan["slots"]):
        last = n == len(plan["slots"]) - 1
        if by_seconds:
            a, b = t, t + float(s["seconds"] or 1)
        else:
            k = max(1, round(s["beats"] * 2))
            a, b = halves[i], halves[min(i + k, len(halves) - 1)]
        if b > plan["max_seconds"] and not last:
            continue
        slots.append({**s, "t0": a, "t1": b})
        if by_seconds:
            t = b
        else:
            i += k
    duration = slots[-1]["t1"]

    canvases, cuts = {}, {}
    for s in slots:
        if s["image"] not in canvases:
            canvases[s["image"]] = make_canvas(Path(s["image"]), plan["mid"])
        if s["effect"] == "parallax" and s["image"] not in cuts:
            p = Path(s["image"])
            cuts[s["image"]] = cutout(p, reels_dir(plan["mid"]) / "cutouts" / f"{p.parent.name}-{p.stem}.png")
            if cuts[s["image"]] is not None:
                canvases["blur:" + s["image"]] = blur_canvas(p)

    tx = plan["texts"]
    # Text goes in two bands: the hook (and top lines) stacks down from the top of the safe area,
    # title/price/CTA stack up from its bottom. The photo stays full screen; a top-band line that would
    # cover the model's face in any shot it spans moves to the bottom band instead.
    styles_ = {
        "hook": dict(size=74, box=(0, 0, 0, 165), group="top"),
        "title": dict(size=54, box=(0, 0, 0, 140), group="bottom"),
        "price": dict(size=84, box=(122, 31, 61, 235), group="bottom"),
        "cta": dict(size=70, box=(0, 0, 0, 185), group="bottom"),
    }
    outline = plan.get("text_style") == "outline"
    texts = []

    def add(text: str, kind: str | None, t_a: float, t_b: float, size=None, group=None):
        st = styles_.get(kind) or dict(size=62, box=(0, 0, 0, 160), group=group or "top")
        if kind == "cta" or (kind == "price" and t_b >= duration):   # the closing price + CTA: last seconds only
            t_a = max(t_a, duration - CTA_SECONDS)
        box = None if outline and kind != "price" else st["box"]
        texts.append(Overlay(text_image(text, size or st["size"], box=box), t_a, t_b, 0.5, group=st["group"]))

    for s in slots:
        for kind in ([s["text"]] if isinstance(s["text"], str) else s["text"] or []):
            if kind == "cta":         # price under the CTA: placed first so it ends up lowest
                if tx["price"]:
                    add(tx["price"], "price", s["t0"] + 0.25, s["t1"] + 1, size=64)
                add(tx["cta"], "cta", s["t0"], s["t1"] + 1)
            elif kind in styles_ and tx.get(kind):
                add(tx[kind], kind, s["t0"], s["t1"])
    # copied styles: text over a range of shots (their original height only picks top or bottom band)
    kept = {s["n"]: s for s in slots}
    copied_price = any(p["text"] == "price" for p in plan.get("overlays") or [])
    for o in plan.get("overlays") or []:
        span = [kept[n] for n in range(int(o["from"]), int(o["to"])) if n in kept]
        if not span:
            if o["text"] != "cta":
                continue
            span = [slots[-1]]            # the CTA always shows, even if its shot was cut for length
        t_a, t_b = span[0]["t0"], span[-1]["t1"] + (1 if span[-1] is slots[-1] else 0)
        kind = o["text"] if o["text"] in styles_ else None
        text = tx.get(kind, "") if kind else o["text"]
        if not text:
            continue
        if kind == "cta" and tx["price"] and not copied_price:
            add(tx["price"], "price", t_a + 0.25, t_b, size=64)
        add(text, kind, t_a, t_b, group="top" if float(o.get("y") or 0.2) < 0.5 else "bottom")
    def stack():                      # within each band, against texts shown at the same time
        for i, o in enumerate(texts):
            h = o.im.size[1]
            same = [p for p in texts[:i] if p.group == o.group and p.start < o.end and p.end > o.start]
            if o.group == "top":
                top = max([p.top + p.im.size[1] + GAP for p in same], default=H * SAFE_TOP)
            else:
                top = min([p.top - GAP for p in same], default=H * SAFE_BOTTOM) - h
            o.y = (top + h / 2) / H

    stack()
    faces = [face_zone(s, canvases[s["image"]], cuts.get(s["image"]), plan["mid"]) for s in slots]
    moved = False
    for o in texts:
        if o.group == "top" and any(s["t0"] < o.end and s["t1"] > o.start and o.top < fb and o.top + o.im.size[1] > ft
                                    for s, (ft, fb) in zip(slots, faces)):
            o.group, moved = "bottom", True
    if moved:
        stack()
    overlays = list(texts)
    if plan.get("brand"):
        overlays.append(Overlay(text_image(plan["brand"], 34, box=None, color=(255, 255, 255)), 0, duration + 1,
                                0.955, pop=False))

    n_frames = int(round(duration * FPS))
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-"]
    if track:
        cmd += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(track)]
    else:
        cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
    fade = max(0.0, duration - 0.8)
    cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
            "-af", f"afade=t=in:d=0.15,afade=t=out:st={fade:.2f}:d=0.8", "-shortest",
            "-movflags", "+faststart", str(out)]
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    prev_last, k = None, 0
    try:
        for idx, s in enumerate(slots):
            f0, f1 = int(round(s["t0"] * FPS)), int(round(s["t1"] * FPS))
            if idx == len(slots) - 1:
                f1 = n_frames
            canvas, cut = canvases[s["image"]], cuts.get(s["image"])
            for f in range(f0, f1):
                t = (f - f0) / max(1, f1 - f0 - 1)
                if s["effect"] == "parallax" and cut is not None:
                    frame = parallax_frame(canvases["blur:" + s["image"]], cut, t, s["dir"])
                else:
                    frame = frame_from_canvas(canvas, *camera(s["effect"], t, s["dir"]))
                j = f - f0
                tr = s["transition"]
                if tr == "flash" and j < 5:
                    frame = Image.blend(frame, Image.new("RGB", (W, H), (255, 255, 255)), 1 - j / 5)
                elif tr == "fade" and prev_last is not None and j < 8:
                    frame = Image.blend(prev_last, frame, (j + 1) / 9)
                elif tr == "whip" and prev_last is not None and j < 6:
                    off = int(W * (1 - ease((j + 1) / 6)))
                    canvas2 = Image.new("RGB", (W, H))
                    canvas2.paste(prev_last, (off - W, 0))
                    canvas2.paste(frame, (off, 0))
                    frame = canvas2
                for ov in overlays:
                    ov.draw(frame, f / FPS)
                proc.stdin.write(frame.tobytes())
                if f == f1 - 1:
                    prev_last = frame
                k += 1
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError("ffmpeg failed")
    except Exception:
        proc.kill()
        raise
    # thumbnail: middle of the first slot that shows the real product
    thumb = next((s for s in slots if s["image"] in {str(p) for p in product_assets(plan["mid"])["real"][:1]}), slots[0])
    log.info("reel %s %s: %.1fs, %d frames in %.0fs", plan["mid"], plan["template"], duration, k, time.time() - t0)
    return {"file": str(out), "seconds": round(duration, 1), "track": Path(plan["track"]).stem if plan["track"] else "",
            "thumb_ms": int((thumb["t0"] + thumb["t1"]) / 2 * 1000), "render_s": round(time.time() - t0)}


def make_reel(mid: str, prow: dict, template: str | None = None, seed: int | None = None) -> dict:
    """Plan + render; remembers the result in data/reels.json under a short reel id."""
    cfg = templates()
    template = template or cfg["defaults"].get("template") or next(iter(cfg["templates"]))
    if template not in cfg["templates"]:
        raise KeyError(f"unknown template {template!r}; have: {', '.join(cfg['templates'])}")
    seed = seed if seed is not None else random.randint(1, 10**9)
    rid = f"{mid}-{int(time.time()) % 10**7:x}"
    plan = plan_reel(mid, prow, template, seed)
    res = render(plan, reels_dir(mid) / f"{rid}.mp4")
    state = load_state()
    state[rid] = {"mid": mid, "template": template, "seed": seed, **res, "status": "DRAFT",
                  "created": time.strftime("%Y-%m-%d %H:%M")}
    save_state(state)
    return {"reel_id": rid, **state[rid]}


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


# --- posting --------------------------------------------------------------------------------------
REEL_COLUMNS = ["reel_id", "meesho_id", "status", "template", "title", "price", "link", "caption", "seconds",
                "track", "ig_media_id", "ig_permalink", "fb_post_id", "fb_permalink", "posted_at", "dms_sent",
                "error", "updated_at"]


class ReelSheet:
    """The "Reels" tab: one row per posted reel (comment->DM reads it like the Posts tab)."""

    def __init__(self, book):
        import gspread
        try:
            self.ws = book.worksheet("Reels")
        except gspread.WorksheetNotFound:
            self.ws = book.add_worksheet("Reels", rows=500, cols=len(REEL_COLUMNS))
            self.ws.update(range_name="A1", values=[REEL_COLUMNS])
            self.ws.freeze(rows=1, cols=2)

    def all(self) -> list[dict]:
        return self.ws.get_all_records(expected_headers=REEL_COLUMNS)

    def upsert(self, rid: str, **fields) -> None:
        from gspread.utils import rowcol_to_a1
        from .sheet import now
        fields["updated_at"] = now()
        ids = self.ws.col_values(1)
        if rid not in ids:
            row = {c: "" for c in REEL_COLUMNS}
            row.update(reel_id=rid, **fields)
            self.ws.append_row([row[c] for c in REEL_COLUMNS], value_input_option="RAW", table_range="A1")
            return
        r = ids.index(rid) + 1
        self.ws.batch_update([{"range": rowcol_to_a1(r, REEL_COLUMNS.index(k) + 1), "values": [[v]]}
                              for k, v in fields.items()], value_input_option="RAW")


def reel_caption(mid: str, prow: dict, link: str, track: str = "") -> str:
    from .music import credit_line
    from .posts import audience, caption
    text = caption(mid, audience(mid, prow), prow.get("my_price", ""), link)
    credit = credit_line(track) if track else ""
    return f"{text}\n.\n{credit}" if credit else text


# --- daily auto reels -----------------------------------------------------------------------------
def auto_cfg() -> dict:
    return templates().get("auto") or {}


def pick_products(rows: list[dict], n: int) -> list[dict]:
    """Live, in-stock products without a recent auto reel. Products whose AI carousel images were made in
    the last 4 days come first (newest first), so each new carousel gets a matching reel; after that,
    products with any AI images are 4x as likely as ones with only Meesho photos."""
    from datetime import datetime, timedelta
    from .posts import post_images, styles
    from .workflow import Pipeline
    cfg = auto_cfg()
    since = (datetime.now() - timedelta(days=int(cfg.get("min_days_between", 30)))).strftime("%Y-%m-%d")
    recent = {r["mid"] for r in load_state().values()
              if r.get("created", "") >= since and r.get("status") != "DISCARDED"}
    skip = [k.lower() for k in styles().get("skip_keywords") or []]
    pool = []
    for r in rows:
        mid = str(r.get("meesho_id") or "")
        title = f"{r.get('shopify_title', '')} {r.get('meesho_title', '')}".lower()
        if (not mid or mid in recent or r.get("status") != "PUSHED" or str(r.get("shopify_status")).upper() != "ACTIVE"
                or str(r.get("in_stock")).upper() in ("FALSE", "NO", "0") or any(k in title for k in skip)):
            continue
        pool.append((r, 4 if post_images(mid) or Pipeline.ai_images(mid) else 1))
    picks, rng = [], random.Random()
    fresh_after = time.time() - 4 * 86400
    fresh = sorted(((max(p.stat().st_mtime for p in post_images(str(r["meesho_id"]))), r) for r, _ in pool
                    if post_images(str(r["meesho_id"]))), key=lambda x: -x[0])
    for mtime, r in fresh:                      # new AI carousels first
        if mtime < fresh_after or len(picks) >= n:
            break
        picks.append(r)
        pool = [x for x in pool if x[0] is not r]
    while pool and len(picks) < n * 3:          # extra candidates, in case some have no photos saved
        r, w = rng.choices(pool, weights=[w for _, w in pool])[0]
        pool = [x for x in pool if x[0] is not r]
        try:
            product_assets(str(r["meesho_id"]))
        except Exception:
            continue
        picks.append(r)
    return picks


def pick_style() -> str:
    """The allowed style used least in the last 2 weeks (ties broken at random)."""
    from datetime import datetime, timedelta
    names = auto_cfg().get("styles") or list(templates()["templates"])
    since = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    used = {n: 0 for n in names}
    for r in load_state().values():
        if r.get("created", "") >= since and r.get("template") in used:
            used[r["template"]] += 1
    low = min(used.values())
    return random.choice([n for n, c in used.items() if c == low])


def approve(rid: str) -> None:
    state = load_state()
    state[rid]["status"] = "APPROVED"
    state[rid]["approved_at"] = time.strftime("%Y-%m-%d %H:%M")
    save_state(state)


def next_approved() -> str | None:
    state = load_state()
    ready = [(r.get("approved_at", ""), rid) for rid, r in state.items()
             if r.get("status") == "APPROVED" and Path(r.get("file", "")).exists()]
    return min(ready)[1] if ready else None


def publish(rid: str, products, reel_sheet) -> dict:
    """Host the mp4 on Shopify, post it as an Instagram Reel (+ Facebook Page Reel), log it in the Reels tab."""
    from datetime import datetime
    from . import facebook, instagram
    from .posts import post_without_link, product_link, store_url
    from .publisher import facebook_enabled
    from .shopify import Shopify
    state = load_state()
    r = state[rid]
    mid = r["mid"]
    prow = products.get(mid) or {}
    link = product_link(prow)
    if not link and not post_without_link():
        return {"no_link": True}
    text = reel_caption(mid, prow, link or store_url(), r.get("track", ""))
    title = str(prow.get("shopify_title") or mid)
    from .publisher import export_ready, manual_mode
    if manual_mode():
        reel_sheet.upsert(rid, meesho_id=mid, status="READY", template=r["template"], title=title,
                          price=prow.get("my_price", ""), link=link, caption=text, seconds=r.get("seconds", ""),
                          track=r.get("track", ""), error="")
        r["status"] = "READY"
        save_state(state)
        return {"manual": export_ready("r", rid, [r["file"]], text)}
    url = Shopify().host_video(Path(r["file"]), alt=title)
    ig = instagram.publish_reel(url, text, int(r.get("thumb_ms") or 0))
    reel_sheet.upsert(rid, meesho_id=mid, status="POSTED", template=r["template"], title=title,
                      price=prow.get("my_price", ""), link=link, caption=text, seconds=r.get("seconds", ""),
                      track=r.get("track", ""), ig_media_id=ig["media_id"], ig_permalink=ig["permalink"],
                      posted_at=datetime.now().strftime("%Y-%m-%d %H:%M"), error="")
    res = {"mid": mid, "permalink": ig["permalink"], "no_product_link": not link}
    if facebook_enabled():
        try:
            fb = facebook.publish_reel(url, text)
            reel_sheet.upsert(rid, fb_post_id=fb["post_id"], fb_permalink=fb["permalink"])
            res["fb_permalink"] = fb["permalink"]
        except Exception as e:   # the Instagram reel stands; report Facebook separately
            log.exception("facebook reel %s failed", rid)
            reel_sheet.upsert(rid, error=f"facebook: {e}"[:300])
            res["fb_error"] = str(e)
    r["status"] = "POSTED"
    save_state(state)
    return {"posted": res}
