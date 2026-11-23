"""Licensed background music for auto-posted reels, filled weekly from Openverse.

Openverse (openverse.org, run by WordPress) indexes Creative Commons audio; we only take tracks under
CC BY, CC0 or Public Domain: those allow business use and editing (cutting to the beat, mixing under a
video). ND (no derivatives), NC (non-commercial) and SA (share-alike) licences are skipped. CC BY needs
credit, so every track's credit line is kept in music/credits.json and added to the reel caption.

Each candidate is checked before it's kept: 30 s-7 min long, a clear steady beat (70-170 BPM) and not
too quiet. Indian-sounding searches come first, then general fashion/upbeat ones.
"""
import json
import logging
import random
import re
import time
from datetime import date

import requests

from .config import DATA_DIR
from .reels import MUSIC_DIR

log = logging.getLogger(__name__)

API = "https://api.openverse.org/v1/audio/"
UA = {"User-Agent": "NoorNakshReelBot/1.0 (small shop reel music; contact via openverse.org)"}
CREDITS_FILE = MUSIC_DIR / "credits.json"
LIBRARY_FILE = DATA_DIR / "music_library.json"
LICENSES = "by,cc0,pdm"
# searched in this order each week; the first group sounds Indian, the rest suits fashion reels
QUERIES = [["bollywood", "indian", "desi", "sitar", "tabla", "bhangra", "raga", "bansuri", "india", "hindi",
            "mumbai", "ethnic", "oriental", "world music", "festive", "wedding"],
           ["fashion", "celebration", "upbeat", "dance", "groove", "lounge", "happy", "romantic", "chill",
            "runway", "pop", "electronic", "house", "funk", "summer"]]


def credits() -> dict:
    return json.loads(CREDITS_FILE.read_text(encoding="utf-8")) if CREDITS_FILE.exists() else {}


def credit_line(track_stem: str) -> str:
    """Caption credit for a track ('' for our own/unknown tracks and public domain)."""
    c = credits().get(track_stem)
    if not c or c["license"] in ("cc0", "pdm"):
        return ""
    return f"🎵 Music: {c['title']} by {c['creator']} ({c['license_label']})"


def _library() -> dict:
    return json.loads(LIBRARY_FILE.read_text(encoding="utf-8")) if LIBRARY_FILE.exists() else {"seen": {}, "weeks": {}}


def _search(q: str, page: int) -> list[dict]:
    r = requests.get(API, params={"q": q, "license": LICENSES, "category": "music", "page_size": 20, "page": page},
                     headers=UA, timeout=60)
    if r.status_code in (400, 404):      # past the last page
        return []
    r.raise_for_status()
    return r.json().get("results", [])


def _check(path) -> tuple[bool, str, float]:
    """(ok, reason, bpm): a steady beat we can cut to, and not too quiet."""
    import librosa
    import numpy as np
    y, sr = librosa.load(str(path), sr=22050, mono=True, duration=120)
    if len(y) < 25 * sr:
        return False, "too short", 0
    rms = float(np.sqrt(np.mean(y ** 2)))
    if rms < 0.03:
        return False, f"too quiet ({rms:.3f})", 0
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    _, beats = librosa.beat.beat_track(onset_envelope=onset, sr=sr, units="time")
    if len(beats) < 30:
        return False, "no clear beat", 0
    gaps = np.diff(beats)
    bpm = 60 / float(np.median(gaps))
    if not 70 <= bpm <= 170:
        return False, f"tempo {bpm:.0f} BPM", bpm
    if float(np.std(gaps) / np.mean(gaps)) > 0.25:
        return False, "unsteady beat", bpm
    return True, "", bpm


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "track"


def fill(n: int = 15) -> dict:
    """Download up to n new checked tracks into music/. Returns {added: [...], rejected: int}."""
    MUSIC_DIR.mkdir(exist_ok=True)
    lib, cred = _library(), credits()
    added, rejected = [], 0
    indian_quota = max(1, int(n * 0.6))     # aim for ~60% Indian-sounding when the pool allows
    for group_i, group in enumerate(QUERIES):
        queries = group[:]
        random.shuffle(queries)
        for q in queries:
            for page in (1, 2, 3):
                if len(added) >= n or (group_i == 0 and len(added) >= indian_quota):
                    break
                try:
                    results = _search(q, page)
                except Exception as e:
                    log.warning("openverse %s p%d: %s", q, page, e)
                    break
                for x in results:
                    if len(added) >= n or (group_i == 0 and len(added) >= indian_quota):
                        break
                    if x["id"] in lib["seen"] or x.get("license") not in ("by", "cc0", "pdm"):
                        continue
                    dur = (x.get("duration") or 0) / 1000
                    lib["seen"][x["id"]] = "seen"
                    if not 30 <= dur <= 420:
                        continue
                    stem = f"{_slug(x['title'])}-{_slug(x.get('creator') or '')[:20]}"
                    if any(c["title"].lower() == x["title"].lower() and c["creator"] == x.get("creator")
                           for c in cred.values()):
                        continue                      # same song listed twice
                    if stem in cred:
                        stem = f"{stem}-{x['id'][:6]}"
                    path = MUSIC_DIR / f"{stem}.mp3"
                    try:
                        r = requests.get(x["url"], headers=UA, timeout=120)
                        r.raise_for_status()
                        if len(r.content) < 100_000:
                            raise RuntimeError("file too small")
                        path.write_bytes(r.content)
                        ok, why, bpm = _check(path)
                    except Exception as e:
                        ok, why, bpm = False, f"download: {e}"[:80], 0
                    if not ok:
                        path.unlink(missing_ok=True)
                        lib["seen"][x["id"]] = f"rejected: {why}"
                        rejected += 1
                        continue
                    label = "CC0" if x["license"] == "cc0" else "Public Domain" if x["license"] == "pdm" \
                        else f"CC BY {x.get('license_version') or ''}".strip()
                    cred[stem] = {"title": x["title"], "creator": x.get("creator") or "unknown",
                                  "license": x["license"], "license_label": label,
                                  "license_url": x.get("license_url") or "", "page": x.get("foreign_landing_url") or "",
                                  "source": x.get("source") or "", "search": q, "bpm": round(bpm),
                                  "added": date.today().isoformat()}
                    lib["seen"][x["id"]] = f"added: {stem}"
                    added.append({"stem": stem, "search": q, "bpm": round(bpm), **cred[stem]})
                    log.info("music: + %s (%s, %d BPM)", stem, label, bpm)
                    time.sleep(1)
                if len(results) < 20:
                    break
        if len(added) >= n:
            break
    CREDITS_FILE.write_text(json.dumps(cred, indent=1, ensure_ascii=False), encoding="utf-8")
    lib["weeks"][date.today().isoformat()] = [a["stem"] for a in added]
    LIBRARY_FILE.write_text(json.dumps(lib, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"added": added, "rejected": rejected, "total": len(cred)}


def report(r: dict) -> str:
    lines = [f"🎶 Music library: +{len(r['added'])} licensed tracks ({r['total']} in total, "
             f"{r['rejected']} skipped for no steady beat / too quiet)."]
    for a in r["added"]:
        lines.append(f"• {a['title'][:40]} – {a['creator'][:30]} · {a['bpm']} BPM · {a['license_label']}")
    lines.append("All CC BY / CC0: free for business use; the credit is added to the reel caption automatically.")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(report(fill()))
