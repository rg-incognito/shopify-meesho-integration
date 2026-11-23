"""Copy the structure of a viral reel into a reel template (reel_templates_copied.yaml).

Only the structure is copied: shot lengths (in beats when the cuts follow the music, else seconds),
camera moves, transitions, where text appears and what it says (rewritten for our product). The
footage and the audio are never reused; reels are rendered from our own product photos by reels.py.

Steps (all CPU): download (Telegram file or a link via yt-dlp) -> cuts (PySceneDetect + a dissolve
check) -> per shot: camera move (ORB + affine fit) and the transition into it (flash / fade / whip /
cut) -> beats (librosa) and how many cuts land on them -> Gemini reads the on-screen text, shot
framing and audio -> template.
"""
import json
import logging
import re
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np
import yaml

from .config import DATA_DIR, ROOT

log = logging.getLogger(__name__)

COPIED_FILE = ROOT / "reel_templates_copied.yaml"
VIRAL_DIR = DATA_DIR / "viral"
LINK_RE = re.compile(r"https?://(?:www\.|m\.)?(?:instagram\.com/(?:[\w.]+/)?(?:reels?|p|tv)/[\w-]+"
                     r"|youtube\.com/shorts/[\w-]+|youtu\.be/[\w-]+|(?:vm\.|vt\.|www\.)?tiktok\.com/\S+)\S*", re.I)
SW, SH = 180, 320          # analysis frame size (9:16)
ROLES = ("hook", "title", "price", "cta")


# --- input ----------------------------------------------------------------------------------------
def download(url: str, dest: Path) -> Path:
    """Public reel/short -> mp4 via yt-dlp. Instagram often asks for a login: then send the file instead."""
    import yt_dlp
    dest.mkdir(parents=True, exist_ok=True)
    opts = {"outtmpl": str(dest / "source.%(ext)s"), "format": "mp4/bestvideo*+bestaudio/best",
            "merge_output_format": "mp4", "quiet": True, "no_warnings": True, "noplaylist": True,
            "max_filesize": 150 * 1024 * 1024}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except Exception as e:
        msg = str(e)
        if "login" in msg.lower() or "rate" in msg.lower() or "cookies" in msg.lower():
            raise RuntimeError("Instagram wants a login to download this one. In the Instagram app tap "
                               "Share → Download (or screen-record it) and send me the video file instead.") from e
        raise RuntimeError(f"couldn't download that link: {msg[:200]}") from e
    if not path.exists():
        path = next(dest.glob("source.*"))
    return path


# --- video analysis -------------------------------------------------------------------------------
def read_frames(path: Path) -> tuple[list[np.ndarray], float]:
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(cv2.resize(f, (SW, SH), interpolation=cv2.INTER_AREA))
    cap.release()
    if len(frames) < fps:
        raise RuntimeError("couldn't read the video (too short or unknown format)")
    return frames, float(fps)


def _hist(f: np.ndarray) -> np.ndarray:
    h = cv2.calcHist([cv2.cvtColor(f, cv2.COLOR_BGR2HSV)], [0, 1], None, [24, 16], [0, 180, 0, 256])
    return cv2.normalize(h, h).flatten()


def _gray(f):
    return cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)


def signals(frames: list[np.ndarray]) -> dict:
    """Per frame (vs the previous one): mean pixel change, sideways shift (phase correlation, as a
    fraction of the width) and its confidence, brightness."""
    g = [np.float32(_gray(f)) for f in frames]
    bright = np.array([float(x.mean()) for x in g])
    win = cv2.createHanningWindow((SW, SH), cv2.CV_32F)
    diff, shift, conf = [0.0], [0.0], [1.0]
    for a, b in zip(g, g[1:]):
        diff.append(float(np.abs(b - a).mean()))
        (dx, _), r = cv2.phaseCorrelate(a.copy(), b.copy(), win)   # copies: it can window its inputs in place
        shift.append(dx / SW)
        conf.append(float(r))
    return {"diff": np.array(diff), "shift": np.array(shift), "conf": np.array(conf), "bright": bright}


def detect_cuts(path: Path, frames: list[np.ndarray], sig: dict) -> list[tuple[int, str]]:
    """Frames where a new shot starts, labelled by how it was found: whip | hard | soft (dissolve)."""
    from scenedetect import AdaptiveDetector, detect
    n, diff = len(frames), sig["diff"]
    found = [(s[0].get_frames(), "hard") for s in detect(str(path), AdaptiveDetector(min_scene_len=4))]
    # whip pans: 3+ frames in a row with the whole picture sliding sideways fast
    fast = (np.abs(sig["shift"]) > 0.08) & (sig["conf"] > 0.2)
    i = 1
    while i < n:
        if fast[i]:
            j = i
            while j < n and fast[j]:
                j += 1
            if j - i >= 3:
                found.append((i, "whip"))
            i = j
        i += 1
    # dissolves/fades: frames 6 apart look very different, but no single frame jump is big
    hists = [_hist(f) for f in frames]
    k = 6
    for i in range(k, n - k):
        if any(abs(i - c) <= 2 * k for c, _ in found):
            continue
        far = cv2.compareHist(hists[i - k], hists[i + k], cv2.HISTCMP_BHATTACHARYYA)
        steps = [cv2.compareHist(hists[j - 1], hists[j], cv2.HISTCMP_BHATTACHARYYA) for j in range(i - k + 1, i + k + 1)]
        if far > 0.45 and max(steps) < far * 0.45:
            found.append((i, "soft"))
    # one cut per transition (a whip or dissolve can trip several detectors); snap to where it starts
    rank = {"whip": 0, "hard": 1, "soft": 2}
    cuts = []
    for c, how in sorted(found):
        if c <= 0:
            continue
        if how == "soft":      # hard cuts and whips are exact already; a dissolve starts where change rises
            lo, hi = max(1, c - 6), min(n, c + 7)
            base = float(np.median(diff[max(1, c - 14):lo])) if lo > 2 else 0.0
            peak = diff[lo:hi].max()
            if peak - base > 2:
                c = next(x for x in range(lo, hi) if diff[x] - base > 0.4 * (peak - base))
        if cuts and c - cuts[-1][0] <= 8:
            if rank[how] < rank[cuts[-1][1]]:
                cuts[-1] = (min(c, cuts[-1][0]), how)
            continue
        cuts.append((c, how))
    return cuts


def camera_move(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float] | None:
    """(scale, dx, dy) taking frame a to frame b (dx/dy as fractions of the frame), None if no fit."""
    orb = cv2.ORB_create(600)
    ka, da = orb.detectAndCompute(_gray(a), None)
    kb, db = orb.detectAndCompute(_gray(b), None)
    if da is None or db is None or len(ka) < 12 or len(kb) < 12:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    if len(matches) < 12:
        return None
    pa = np.float32([ka[m.queryIdx].pt for m in matches])
    pb = np.float32([kb[m.trainIdx].pt for m in matches])
    M, inl = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=3)
    if M is None or inl is None or inl.sum() < 10:
        return None
    s = float(np.hypot(M[0, 0], M[1, 0]))
    c = np.array([SW / 2, SH / 2])
    d = M[:, :2] @ c + M[:, 2] - c
    return s, float(d[0] / SW), float(d[1] / SH)


def shot_effect(frames: list[np.ndarray], a: int, b: int) -> str:
    """Our nearest camera effect for frames [a, b)."""
    n = b - a
    if n < 5:
        return "punch"
    m = max(1, n // 10)
    whole = camera_move(frames[a + m], frames[b - 1 - m])
    early = camera_move(frames[a], frames[a + min(n // 3, 8)])
    if early and abs(early[0] - 1) > 0.06 and (not whole or abs(whole[0] - 1) < abs(early[0] - 1) * 1.5):
        return "punch"              # a snap zoom right at the start, then (almost) still
    if not whole:
        return "drift"              # the subject moves (a person walking/twirling): gentle motion
    s, dx, dy = whole
    if abs(s - 1) > 0.04 and abs(s - 1) >= max(abs(dx), abs(dy)):
        return "zoom_in" if s > 1 else "zoom_out"
    if abs(dy) > 0.04 and abs(dy) >= abs(dx):
        return "pan_down" if dy < 0 else "pan_up"      # content moving up = camera moving down
    if abs(dx) > 0.04:
        return "drift"
    return "still"


def transition_into(sig: dict, c: int, how: str) -> str:
    """How the shot starting at frame c begins: flash | fade | whip | cut."""
    if how == "whip":
        return "whip"
    b, d, n = sig["bright"], sig["diff"], len(sig["diff"])
    at = lambda i: b[max(0, min(n - 1, i))]   # noqa: E731
    around = max(at(c + j) for j in range(-1, 3))
    base = max(np.mean([at(c - j) for j in range(3, 6)]), np.mean([at(c + j) for j in range(6, 9)]))
    if around > 200 and around - base > 40:
        return "flash"
    if min(at(c - 1), at(c)) < 18:
        return "fade"               # through black
    base = float(np.median(d[max(1, c - 10):c])) if c > 2 else 0.0    # normal motion inside the shot
    win = d[c:min(n, c + 10)] - base
    if how == "soft" or (len(win) and win.max() < 60 and (win > 0.4 * win.max()).sum() >= 4):
        return "fade"               # the change is spread over several frames: a dissolve
    return "cut"


# --- audio ----------------------------------------------------------------------------------------
def beats_of(path: Path, workdir: Path) -> dict:
    """Beat times of the reel's own audio (empty if silent / no audio)."""
    wav = workdir / "audio.wav"
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "22050",
                        str(wav)], capture_output=True)
    if r.returncode != 0 or not wav.exists():
        return {"beats": [], "bpm": 0}
    import librosa
    y, sr = librosa.load(str(wav), sr=22050, mono=True)
    wav.unlink(missing_ok=True)
    if len(y) < sr or float(np.sqrt(np.mean(y ** 2))) < 0.005:
        return {"beats": [], "bpm": 0}
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
    beats = [float(b) for b in beats]
    bpm = 60 / float(np.median(np.diff(beats))) if len(beats) > 3 else float(np.atleast_1d(tempo)[0])
    return {"beats": beats, "bpm": round(bpm, 1)}


def beat_sync(cut_times: list[float], beats: list[float]) -> tuple[int, float]:
    """How many cuts land on a beat or half-beat (within 80 ms), and the beat period."""
    if len(beats) < 4 or not cut_times:
        return 0, 0.0
    period = float(np.median(np.diff(beats)))
    grid = np.array(sorted(beats + [b + period / 2 for b in beats]))
    hits = sum(1 for t in cut_times if np.min(np.abs(grid - t)) < 0.08)
    return hits, period


# --- Gemini: on-screen text, framing, audio ------------------------------------------------------
GEMINI_PROMPT = """You are analysing a short vertical video (an Instagram Reel) that performed well, so that
{store}, a small Indian ethnic-wear store, can reuse its STRUCTURE with its own product photos. Their
footage and audio will not be copied, only the pacing, text placement and type of lines.

Return JSON:
- name: a 1-3 word snake_case name for this format (e.g. guess_the_price, pov_festive, outfit_check).
- summary: 1-2 sentences: what happens in the reel and why it holds attention.
- text_style: "box" if on-screen text sits on coloured/dark boxes, "outline" if it's plain text with an
  outline/shadow, "none" if there's no on-screen text.
- texts: every on-screen text overlay in order (ignore the Instagram UI and small watermarks): start and
  end in seconds; text exactly as shown; role = hook (the first attention line), price, title (product
  name), cta (comment/follow/link/shop call to action) or line (anything else); y = the vertical centre
  of the text as a fraction of the height (0 top, 1 bottom); ours = the same line rewritten for OUR
  product in the same tone, language (English/Hinglish) and length, using {{short}} for our product name
  and ₹{{price}} for its price where it fits. Never write any other amount or number of rupees (we don't
  know the price in advance), and never mention the original creator or brand.
- shots: for each shot, its start in seconds and framing: full (whole outfit), closeup (upper body),
  detail (fabric/embroidery/jewellery close-up), back, walk (person moving/twirling), product (flat lay,
  hanger, mannequin) or other.
- hooks: 3 alternative opening lines in the same style for our product (use {{short}} / ₹{{price}}, no
  other amounts).
- audio: what the audio is: the song and artist if you recognise it, otherwise mood/genre/tempo; say if
  there's a voice-over.
- tips: up to 3 short points on why this reel works (pacing, reveal, text)."""

GEMINI_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "summary": {"type": "string"},
        "text_style": {"type": "string", "enum": ["box", "outline", "none"]},
        "texts": {"type": "array", "items": {"type": "object", "properties": {
            "start": {"type": "number"}, "end": {"type": "number"}, "text": {"type": "string"},
            "role": {"type": "string", "enum": ["hook", "title", "price", "cta", "line"]},
            "y": {"type": "number"}, "ours": {"type": "string"}},
            "required": ["start", "end", "text", "role", "y", "ours"]}},
        "shots": {"type": "array", "items": {"type": "object", "properties": {
            "start": {"type": "number"},
            "framing": {"type": "string",
                        "enum": ["full", "closeup", "detail", "back", "walk", "product", "other"]}},
            "required": ["start", "framing"]}},
        "hooks": {"type": "array", "items": {"type": "string"}},
        "audio": {"type": "string"},
        "tips": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "summary", "text_style", "texts", "shots", "hooks", "audio", "tips"],
}


def gemini_read(path: Path) -> dict | None:
    """Gemini watches the reel (text, framing, audio). None if every model is out of quota or fails."""
    from google.genai import types
    from .ai import GeminiAI
    from .config import CFG
    try:
        g = GeminiAI()
        f = g.client.files.upload(file=str(path))
        deadline = time.time() + 180
        while getattr(f.state, "name", str(f.state)) == "PROCESSING" and time.time() < deadline:
            time.sleep(3)
            f = g.client.files.get(name=f.name)
    except Exception as e:
        log.warning("gemini upload failed: %s", e)
        return None
    try:
        for model in g._text_models():
            try:
                resp = g._call(model=model, contents=[f, GEMINI_PROMPT.format(store=CFG["store"]["name"])],
                               config=types.GenerateContentConfig(response_mime_type="application/json",
                                                                  response_json_schema=GEMINI_SCHEMA,
                                                                  temperature=0.4))
                data = json.loads(resp.text)
                data["_model"] = model
                return data
            except Exception as e:
                log.warning("gemini %s couldn't read the reel: %s", model, str(e)[:120])
                if "RESOURCE_EXHAUSTED" in str(e):
                    g._exhausted[model] = time.time() + (3 * 3600 if "PerDay" in str(e) else 120)
        return None
    finally:
        try:
            g.client.files.delete(name=f.name)
        except Exception:
            pass


# --- analysis -> template -------------------------------------------------------------------------
def analyse(path: Path, workdir: Path, use_gemini: bool = True) -> dict:
    t0 = time.time()
    frames, fps = read_frames(path)
    n = len(frames)
    sig = signals(frames)
    cuts = detect_cuts(path, frames, sig)
    bounds = [(0, "start")] + [c for c in cuts if 2 < c[0] < n - 2] + [(n, "end")]
    shots = []
    for (a, how), (b, _) in zip(bounds, bounds[1:]):
        if b - a < 3 and shots:          # 1-2 frame blips (flash frames): merge into the previous shot
            shots[-1]["end_f"] = b
            if sig["bright"][a] > 200:
                shots[-1]["flash_after"] = True
            continue
        shots.append({"start_f": a, "end_f": b, "how": how})
    settle = {"whip": 7, "fade": 9, "flash": 6, "cut": 0}   # frames the transition itself takes
    for i, s in enumerate(shots):
        s["start"], s["end"] = round(s["start_f"] / fps, 3), round(s["end_f"] / fps, 3)
        if i == 0:
            s["transition"] = "cut"
        elif shots[i - 1].get("flash_after"):
            s["transition"] = "flash"
        else:
            s["transition"] = transition_into(sig, s["start_f"], s.pop("how"))
        s.pop("how", None)
        a = s["start_f"] + min(settle[s["transition"]], (s["end_f"] - s["start_f"]) // 3)
        s["effect"] = shot_effect(frames, a, s["end_f"])
    audio = beats_of(path, workdir)
    cut_times = [s["start"] for s in shots[1:]]
    hits, period = beat_sync(cut_times, audio["beats"])
    synced = bool(cut_times) and period > 0 and hits >= max(2, 0.6 * len(cut_times))
    gem = gemini_read(path) if use_gemini else None
    return {"seconds": round(n / fps, 2), "fps": fps, "shots": shots, "bpm": audio["bpm"], "beat_period": period,
            "cuts_on_beat": hits, "synced": synced, "gemini": gem, "analysed_in": round(time.time() - t0)}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:24].strip("_")
    return s or "viral"


PRICE_RE = re.compile(r"(₹|rs\.?|inr)\s*\d|\d\s*(/-|rs\b|rupees)", re.I)


def _fill_check(text: str) -> str:
    """Keep only the placeholders our renderer knows (drop anything else in braces); {price} is a bare
    number, so it gets a ₹ in front."""
    text = re.sub(r"\{(?!(short|price|title|brand)\})[^}]*\}", "", text or "")
    return re.sub(r"(?<!₹)\{price\}", "₹{price}", text).strip()


def _honest(text: str) -> bool:
    """False for lines quoting a made-up price (e.g. 'Under ₹999?'): only {price} may state a price."""
    return not PRICE_RE.search(re.sub(r"₹?\{price\}", "", text))


def build_template(an: dict) -> dict:
    gem = an.get("gemini") or {}
    shots, synced, period = an["shots"], an["synced"], an["beat_period"]
    framings = sorted(gem.get("shots") or [], key=lambda x: x["start"])

    def framing_at(t0, t1):
        best = None
        for fr in framings:
            if fr["start"] <= (t0 + t1) / 2 + 0.15:
                best = fr["framing"]
        return best

    slots = []
    for i, s in enumerate(shots):
        fr = framing_at(s["start"], s["end"])
        effect = s["effect"]
        if fr == "detail" and effect in ("still", "zoom_in", "zoom_out"):
            effect = "detail"
        source = "hero" if i == 0 else "real" if fr in ("detail", "closeup", "product") else "any"
        slot = {"source": source, "effect": effect}
        if s["transition"] != "cut":
            slot["transition"] = s["transition"]
        dur = s["end"] - s["start"]
        if synced:
            slot["beats"] = max(0.5, round(dur / period * 2) / 2)
        else:
            slot["seconds"] = round(max(0.25, dur), 2)
        slots.append(slot)

    # the CTA needs time to be read: make the last shot at least ~2.5 s
    last = slots[-1]
    if synced:
        last["beats"] = max(last["beats"], float(np.ceil(2.5 / period)))
    else:
        last["seconds"] = max(last["seconds"], 2.5)

    def shot_index(t: float) -> int:
        for i, s in enumerate(shots):
            if s["start"] - 0.05 <= t < s["end"]:
                return i
        return len(shots) - 1

    def snap(t: float) -> float:
        """Gemini's text times are coarse (about 1 frame per second): text usually changes on a cut."""
        near = min((s["start"] for s in shots), key=lambda c: abs(c - t))
        return near if abs(near - t) <= 0.9 else t

    overlays, hooks, price_text, has_cta = [], [], None, False
    for tx in gem.get("texts") or []:
        t_a = snap(float(tx["start"]))
        t_b = max(t_a + 0.1, snap(float(tx["end"])))
        a = shot_index(t_a)
        b = max(a + 1, shot_index(t_b - 0.05) + 1)
        ours = _fill_check(tx.get("ours", ""))
        if not _honest(ours):
            ours = ""
        role = tx["role"]
        y = round(min(0.9, max(0.08, float(tx.get("y") or 0.5))), 3)
        if role == "hook":
            if ours:
                hooks.append(ours)
            overlays.append({"from": a, "to": b, "text": "hook", "y": y})
        elif role == "price":
            if "{price}" in ours:
                price_text = ours
            overlays.append({"from": a, "to": b, "text": "price", "y": y})
        elif role == "cta":       # keep the viral CTA's timing/place, but always our "Comment LINK" CTA (DM flow)
            has_cta = True
            overlays.append({"from": a, "to": len(shots), "text": "cta", "y": min(y, 0.72)})
        elif role == "title":
            overlays.append({"from": a, "to": b, "text": "title", "y": y})
        elif ours:
            overlays.append({"from": a, "to": b, "text": ours, "y": y})
    if not overlays:              # no text found (or no Gemini): our usual hook + CTA
        k = next((i for i, s in enumerate(shots) if s["end"] >= 2.0), 0) + 1
        overlays.append({"from": 0, "to": min(k, len(shots) - 1) or 1, "text": "hook", "y": 0.17})
    if not has_cta:
        overlays.append({"from": len(shots) - 1, "to": len(shots), "text": "cta", "y": 0.70})
    hooks += [_fill_check(h) for h in gem.get("hooks") or []]
    hooks = [h for h in dict.fromkeys(hooks) if h and _honest(h)] or None

    if synced:
        est = sum(s["beats"] for s in slots) * period
    else:
        est = sum(s["seconds"] for s in slots)
    tpl = {"description": (gem.get("summary") or f"Copied: {len(shots)} shots in {an['seconds']} s")[:200],
           "copied": True, "timing": "beats" if synced else "seconds",
           "max_seconds": float(min(60, round(est + 1))),
           "text_style": {"none": "box"}.get(gem.get("text_style"), gem.get("text_style") or "box")}
    if hooks:
        tpl["hooks"] = hooks
    if price_text:
        tpl["price_text"] = price_text
    tpl["slots"] = slots
    tpl["overlays"] = overlays
    return tpl


# --- storage --------------------------------------------------------------------------------------
def copied() -> dict:
    if not COPIED_FILE.exists():
        return {}
    with open(COPIED_FILE, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("templates") or {}


def save_copied(tpls: dict) -> None:
    header = ("# Reel styles copied from viral reels (written by the bot: send it a reel video or link).\n"
              "# Same fields as reel_templates.yaml, plus:\n"
              "#   timing: beats | seconds   (seconds = the original cuts didn't follow the music)\n"
              "#   overlays: text shown from shot `from` up to (not incl.) shot `to`, centred at height y;\n"
              "#             text = hook | title | price | cta | any line ({short} {price} {title} {brand})\n"
              "#   text_style: box | outline\n\n")
    body = yaml.safe_dump({"templates": tpls}, allow_unicode=True, sort_keys=False, width=110)
    COPIED_FILE.write_text(header + body, encoding="utf-8")


def unique_name(base: str) -> str:
    from .reels import templates
    have = set(templates()["templates"])
    name = f"v_{_slug(base)}"
    k = 2
    while name in have:
        name = f"v_{_slug(base)}{k}"
        k += 1
    return name


def copy_reel(video: Path) -> dict:
    """Analyse a reel video and save it as a new style. Returns {name, template, analysis}."""
    work = VIRAL_DIR / time.strftime("%Y%m%d-%H%M%S")
    work.mkdir(parents=True, exist_ok=True)
    src = work / f"source{video.suffix or '.mp4'}"
    if video.resolve() != src.resolve():
        src.write_bytes(video.read_bytes())
    an = analyse(src, work)
    tpl = build_template(an)
    name = unique_name((an.get("gemini") or {}).get("name") or "viral")
    tpl["source"] = work.name
    tpls = copied()
    tpls[name] = tpl
    save_copied(tpls)
    (work / "analysis.json").write_text(json.dumps({"name": name, **an}, indent=1, ensure_ascii=False),
                                        encoding="utf-8")
    log.info("copied reel %s: %d shots, synced=%s, gemini=%s", name, len(an["shots"]), an["synced"],
             bool(an.get("gemini")))
    return {"name": name, "template": tpl, "analysis": an}


def copy_link(url: str) -> dict:
    tmp = VIRAL_DIR / "_download" / str(int(time.time()))
    path = download(url, tmp)
    try:
        return copy_reel(path)
    finally:
        for p in tmp.glob("*"):
            p.unlink(missing_ok=True)
        tmp.rmdir()


def delete_copied(name: str) -> bool:
    tpls = copied()
    if name not in tpls:
        return False
    del tpls[name]
    save_copied(tpls)
    return True


def report(res: dict) -> str:
    """Telegram summary of what was copied."""
    an, name = res["analysis"], res["name"]
    gem = an.get("gemini") or {}
    shots = an["shots"]
    avg = an["seconds"] / max(1, len(shots))
    pace = "very fast" if avg < 0.7 else "fast" if avg < 1.3 else "medium" if avg < 2.5 else "slow"
    from collections import Counter
    moves = Counter(s["effect"] for s in shots)
    trans = Counter(s["transition"] for s in shots[1:])
    lines = [f"📊 Copied as style: {name}",
             f"• {len(shots)} shots in {an['seconds']} s (avg {avg:.1f} s, {pace})"]
    if an["synced"]:
        lines.append(f"• Cuts on the beat: {an['cuts_on_beat']}/{len(shots) - 1} at {an['bpm']:.0f} BPM "
                     "→ your version cuts on your music's beats")
    elif an["bpm"]:
        lines.append(f"• Cuts don't follow the beat ({an['cuts_on_beat']}/{len(shots) - 1}) → copied in seconds")
    else:
        lines.append("• No music detected → copied in seconds")
    lines.append("• Camera: " + ", ".join(f"{v}× {k.replace('_', ' ')}" for k, v in moves.most_common()))
    if trans:
        lines.append("• Transitions: " + ", ".join(f"{v}× {k}" for k, v in trans.most_common()))
    if gem:
        for tx in gem.get("texts", [])[:6]:
            lines.append(f"• {tx['start']:.1f}s {tx['role']}: “{tx['text'][:60]}” → “{tx.get('ours', '')[:60]}”")
        if gem.get("audio"):
            lines.append(f"🎵 Audio: {gem['audio'][:160]}")
        if gem.get("summary"):
            lines.append(f"\n💡 {gem['summary']}")
        for tip in gem.get("tips", [])[:3]:
            lines.append(f"  – {tip}")
    else:
        lines.append("• Text: not read (Gemini unavailable), using our hook + CTA")
    if not any(t["role"] == "cta" for t in gem.get("texts") or []):
        lines.append("\nℹ️ Added our “Comment LINK” CTA at the end so comment→DM keeps working.")
    lines.append(f"\nTry it: /reel <product> {name}  (edit it in reel_templates_copied.yaml)")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    arg = sys.argv[1]
    r = copy_link(arg) if arg.startswith("http") else copy_reel(Path(arg))
    print(report(r))
