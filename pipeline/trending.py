"""Weekly report: 15 new Hindi/Punjabi songs that are hot right now and suit fashion / ethnic-wear reels.

Nothing is downloaded and Instagram isn't scraped. The signal is Shazam's public charts for India
(country + Mumbai + Delhi, "performance over the past 7 days", CSV): songs people hear in reels and
look up climb there fast. Gemini (plain, no web search: the free key has no search quota) sorts them
by language, fit for fashion reels, mood and how creators use them. You add the song in the Instagram
app when posting (Telegram "Send file" on a reel preview): that's licensed by Instagram and links the
reel to the song's audio page, which is where the trending reach comes from.

History in data/trending_songs.json: songs already reported aren't repeated, and last week's ranks
give the "climbing" arrows.
"""
import csv
import io
import json
import logging
import re
import time
from datetime import date
from urllib.parse import quote_plus

import requests

from .config import DATA_DIR

log = logging.getLogger(__name__)

HISTORY_FILE = DATA_DIR / "trending_songs.json"
CHARTS = {"India": "top-200/india", "Mumbai": "top-200/india/mumbai", "Delhi": "top-200/india/delhi",
          "Discovery": "discovery/india"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"}
LANGS = {"Hindi", "Punjabi", "Urdu", "Haryanvi", "Rajasthani", "Gujarati"}


def _key(title: str, artist: str = "") -> str:
    title = re.sub(r"\(.*?\)|\[.*?\]|feat\..*", "", title or "", flags=re.I)
    return re.sub(r"[^a-z0-9]", "", title.lower()) + "|" + re.sub(r"[^a-z0-9]", "", (artist or "").lower())[:12]


def fetch_charts() -> list[dict]:
    """Songs from all charts, each with its best rank and which charts it's on."""
    songs: dict[str, dict] = {}
    for name, path in CHARTS.items():
        try:
            r = requests.get(f"https://www.shazam.com/services/charts/csv/{path}", headers=UA, timeout=60)
            r.raise_for_status()
        except Exception as e:
            log.warning("shazam chart %s: %s", name, e)
            continue
        lines = r.text.lstrip("﻿").splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith("Rank,")), None)
        if start is None:
            continue
        for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
            try:
                rank = int(row["Rank"])
            except (ValueError, KeyError):
                continue
            k = _key(row["Title"], row["Artist"])
            s = songs.setdefault(k, {"key": k, "title": row["Title"].strip(), "artist": row["Artist"].strip(),
                                     "rank": 999, "charts": {}})
            s["charts"][name] = rank
            if name != "Discovery":
                s["rank"] = min(s["rank"], rank)
    if not songs:
        raise RuntimeError("couldn't read the Shazam charts (network?)")
    return sorted(songs.values(), key=lambda s: (s["rank"], min(s["charts"].values())))


CLASSIFY_PROMPT = """These songs are on Shazam's India charts this week. For each one, using what you
know (say so when you don't know the song), tell a small Indian ethnic-wear store (sarees, lehengas,
kurtis, kids wear, jewellery) whether it suits their Instagram Reels.

For each id return:
- language: Hindi | Punjabi | Urdu | Haryanvi | Rajasthani | Gujarati | Tamil | Telugu | Malayalam |
  Kannada | Bengali | Marathi | English | Spanish | other
- fit: 0-3 for fashion / ethnic-wear reels (3 = perfect for outfit reveals, festive or wedding looks,
  saree/lehenga walks; 0 = unsuitable: aggressive, sad-only, gangster, explicit, instrumental BGM)
- mood: festive | romantic | party | classy | emotional | devotional | swag | other
- use: one short line on how fashion creators could use it in a reel (e.g. "outfit transition on the
  beat drop", "slow-motion dupatta walk on the chorus")
- known: true if you actually know this song, false if you're guessing from the title/artist

Songs (id. title by artist):
{songs}"""

CLASSIFY_SCHEMA = {
    "type": "array",
    "items": {"type": "object", "properties": {
        "id": {"type": "integer"}, "language": {"type": "string"}, "fit": {"type": "integer"},
        "mood": {"type": "string"}, "use": {"type": "string"}, "known": {"type": "boolean"}},
        "required": ["id", "language", "fit", "mood", "use", "known"]},
}


def classify(songs: list[dict]) -> bool:
    """Adds language/fit/mood/use to each song in place. False if Gemini was unavailable."""
    from google.genai import types
    from .ai import GeminiAI
    g = GeminiAI()
    listing = "\n".join(f"{i}. {s['title']} by {s['artist']}" for i, s in enumerate(songs))
    for model in g._text_models():
        try:
            resp = g._call(model=model, contents=CLASSIFY_PROMPT.format(songs=listing),
                           config=types.GenerateContentConfig(response_mime_type="application/json",
                                                              response_json_schema=CLASSIFY_SCHEMA,
                                                              temperature=0.2))
            for c in json.loads(resp.text):
                if 0 <= c["id"] < len(songs):
                    songs[c["id"]].update({k: c[k] for k in ("language", "fit", "mood", "use", "known")})
            return True
        except Exception as e:
            log.warning("classifying songs via %s failed: %s", model, str(e)[:150])
            if "RESOURCE_EXHAUSTED" in str(e):
                g._exhausted[model] = time.time() + (3 * 3600 if "PerDay" in str(e) else 120)
    return False


def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {"reported": {}, "ranks": {}, "weeks": {}}


def weekly(n: int = 15) -> dict:
    hist = load_history()
    charts = fetch_charts()
    last_ranks = hist.get("ranks", {})
    fresh = [s for s in charts if s["key"] not in hist["reported"]][:160]
    classified = classify(fresh)
    for s in fresh:
        prev = last_ranks.get(s["key"])
        s["move"] = (prev - s["rank"]) if prev and s["rank"] < 999 else None
    if classified:
        picks = [s for s in fresh if s.get("language") in LANGS and int(s.get("fit") or 0) >= 2]
        # chart position matters most; a perfect fit counts like being ~2x higher on the chart, climbing fast +10
        picks.sort(key=lambda s: s["rank"] * (1.0 if int(s["fit"]) >= 3 else 1.8)
                   - 10 * bool(s["move"] and s["move"] >= 10))
        if len(picks) < n:   # a quiet week: top up with fit-1 songs
            picks += sorted((s for s in fresh if s.get("language") in LANGS and int(s.get("fit") or 0) == 1),
                            key=lambda s: s["rank"])[:n - len(picks)]
    else:                    # Gemini down: the highest-ranked new songs, unsorted by fit
        picks = list(fresh)
    picks = picks[:n]
    # older songs that are still near the top this week
    still = [s for s in charts if s["key"] in hist["reported"] and s["rank"] <= 40][:6]
    week = date.today().isoformat()
    for s in picks:
        hist["reported"][s["key"]] = {"title": s["title"], "artist": s["artist"], "week": week}
    hist["weeks"][week] = [s["key"] for s in picks]
    hist["ranks"] = {s["key"]: s["rank"] for s in charts if s["rank"] < 999}
    HISTORY_FILE.write_text(json.dumps(hist, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"week": week, "new": picks, "still": still, "classified": classified, "chart_size": len(charts)}


def report(r: dict) -> list[str]:
    """Telegram messages (HTML), split under the 4096-character limit."""
    import html
    e = html.escape
    head = (f"🎵 <b>Trending songs for your reels, week of {r['week']}</b>\n"
            f"{len(r['new'])} new Hindi/Punjabi picks from Shazam India's charts (past 7 days)"
            + (", sorted by how well they suit fashion reels." if r["classified"]
               else ". ⚠️ Gemini was busy, so they aren't checked for language or fit this week.")
            + "\n\nHow to use: /reel → <b>📲 Send file</b> → post it from the Instagram app, tap the music "
              "icon and search the song. Pick the trending part (the clip most reels use).")
    msgs, cur = [], head
    for i, s in enumerate(r["new"], 1):
        q = f"{s['title']} {s['artist']}"
        where = ", ".join(f"#{v} {k}" for k, v in s["charts"].items())
        move = (f" · ↑{s['move']}" if s.get("move") and s["move"] > 0 else "") + \
               (" · 🆕 new on chart" if s.get("move") is None and s["rank"] < 999 else "")
        info = ""
        if r["classified"]:
            stars = "★" * int(s.get("fit") or 0)
            unsure = "" if s.get("known") else " (not sure of this one, listen first)"
            info = (f"\n   👗 {stars} · {e(s.get('mood', ''))} · {e(s.get('language', ''))}{unsure}"
                    f"\n   🎬 {e(s.get('use', ''))}")
        block = (f"\n\n<b>{i}. {e(s['title'])}</b> · {e(s['artist'])}\n   📈 {e(where)}{move}{info}\n"
                 f"   🔎 Instagram: <code>{e(s['title'])}</code> · "
                 f"<a href=\"https://www.youtube.com/results?search_query={quote_plus(q)}\">listen</a>")
        if len(cur) + len(block) > 3800:
            msgs.append(cur)
            cur = ""
        cur += block
    if r["still"]:
        cur += "\n\n🔁 <b>Still hot from earlier weeks:</b> " + ", ".join(
            e(f"{s['title']} (#{s['rank']})") for s in r["still"])
    cur += ("\n\nℹ️ Reels posted from the app aren't tracked for comment→DM: in their caption ask people to "
            "DM you for the link, and reply from the Instagram inbox.")
    msgs.append(cur)
    return msgs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for m in report(weekly()):
        print(m, "\n-----")
