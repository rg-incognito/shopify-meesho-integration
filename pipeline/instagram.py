"""Instagram API with Instagram Login (Meta app "Noor Naksh Posts"): carousel publishing,
token refresh, and comment automation (public reply + private-reply DM with the product link).

The long-lived token comes from IG_ACCESS_TOKEN in .env; refreshed tokens are kept in
data/ig_token.json (valid 60 days, refreshed weekly by the bot).
"""
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from PIL import Image

from .config import DATA_DIR, env

log = logging.getLogger(__name__)

API = "https://graph.instagram.com/v23.0"
TOKEN_FILE = DATA_DIR / "ig_token.json"
COMMENTS_FILE = DATA_DIR / "ig_comments.json"


class InstagramError(RuntimeError):
    pass


def token() -> str:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))["access_token"]
    return env("IG_ACCESS_TOKEN")


def _req(method: str, path: str, **params) -> dict:
    params["access_token"] = token()
    for attempt in range(3):
        try:
            r = requests.request(method, f"{API}/{path.lstrip('/')}",
                                 **({"params": params} if method == "GET" else {"data": params}), timeout=60)
        except requests.RequestException as e:
            if attempt == 2:
                raise InstagramError(str(e)) from e
            time.sleep(5)
            continue
        data = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            if err.get("is_transient") and attempt < 2:
                time.sleep(10)
                continue
            raise InstagramError(f"{path}: {err.get('message') or r.text[:300]}")
        return data
    raise InstagramError(f"{path}: failed after retries")


def me() -> dict:
    return _req("GET", "me", fields="user_id,username,account_type,media_count")


def refresh_token() -> str:
    """Extends the long-lived token by 60 days (allowed once it is >24 h old)."""
    data = requests.get(f"{API.rsplit('/', 1)[0]}/refresh_access_token",
                        params={"grant_type": "ig_refresh_token", "access_token": token()}, timeout=60).json()
    if "access_token" not in data:
        raise InstagramError(f"token refresh failed: {data}")
    data["refreshed_at"] = datetime.now().isoformat(timespec="seconds")
    TOKEN_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return f"token refreshed, valid ~{int(data.get('expires_in', 0)) // 86400} days"


# --- publishing ---------------------------------------------------------------------------------
def to_feed_ratio(src: Path, out: Path) -> Path:
    """Instagram feed images must be between 4:5 and 1.91:1; centre-crop taller images to 4:5."""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    if h / w > 1.25:
        nh = int(w * 1.25)
        top = (h - nh) // 2
        im = im.crop((0, top, w, top + nh))
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "JPEG", quality=95)
    return out


def _wait_container(cid: str, timeout=300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _req("GET", cid, fields="status_code,status").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise InstagramError(f"media container {cid} {st}")
        time.sleep(5)
    raise InstagramError(f"media container {cid} not ready after {timeout}s")


def publish_carousel(image_urls: list[str], caption: str) -> dict:
    """Creates and publishes a carousel (2-10 images; 1 image = single photo post)."""
    uid = me()["user_id"]
    if len(image_urls) == 1:
        cid = _req("POST", f"{uid}/media", image_url=image_urls[0], caption=caption)["id"]
    else:
        children = [_req("POST", f"{uid}/media", image_url=u, is_carousel_item="true")["id"] for u in image_urls]
        for c in children:
            _wait_container(c)
        cid = _req("POST", f"{uid}/media", media_type="CAROUSEL", children=",".join(children),
                   caption=caption)["id"]
    _wait_container(cid)
    mid = _req("POST", f"{uid}/media_publish", creation_id=cid)["id"]
    info = _req("GET", mid, fields="permalink,timestamp")
    return {"media_id": mid, "permalink": info.get("permalink", ""), "timestamp": info.get("timestamp", "")}


def publish_reel(video_url: str, caption: str, thumb_ms: int = 0) -> dict:
    """Publishes a Reel from a public mp4 URL (also shown in the profile grid)."""
    uid = me()["user_id"]
    cid = _req("POST", f"{uid}/media", media_type="REELS", video_url=video_url, caption=caption,
               share_to_feed="true", thumb_offset=str(thumb_ms))["id"]
    _wait_container(cid, timeout=900)
    mid = _req("POST", f"{uid}/media_publish", creation_id=cid)["id"]
    info = _req("GET", mid, fields="permalink,timestamp")
    return {"media_id": mid, "permalink": info.get("permalink", ""), "timestamp": info.get("timestamp", "")}


# --- comment automation -------------------------------------------------------------------------
def _load_state() -> dict:
    if COMMENTS_FILE.exists():
        return json.loads(COMMENTS_FILE.read_text(encoding="utf-8"))
    return {"handled": {}}


def _save_state(state: dict) -> None:
    COMMENTS_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def matches(text: str, keywords: list[str]) -> bool:
    t = (text or "").strip().lower()
    for k in keywords:
        k = k.lower()
        if k.isalnum():
            if re.search(rf"\b{re.escape(k)}\b", t):
                return True
        elif k in t:
            return True
    return False


def handle_comments(posts: list[dict], cfg: dict, my_username: str) -> list[dict]:
    """posts: [{media_id, posted_at(iso), title, price, link}] for recent posts.
    Replies publicly + sends the link by private reply (allowed within 7 days of the comment)."""
    state = _load_state()
    done = []
    keywords = cfg.get("keywords") or ["link"]
    # one call for all comment counts; only fetch comments where the count changed
    counts = {m["id"]: m.get("comments_count", 0)
              for m in _req("GET", "me/media", fields="id,comments_count", limit=100).get("data", [])}
    seen = state.setdefault("counts", {})
    for p in posts:
        n = counts.get(p["media_id"])
        if n is not None and seen.get(p["media_id"]) == n:
            continue
        try:
            comments = _req("GET", f"{p['media_id']}/comments", fields="id,text,username,timestamp",
                            limit=50).get("data", [])
        except InstagramError as e:
            log.warning("comments for %s: %s", p["media_id"], e)
            continue
        seen[p["media_id"]] = n
        for c in comments:
            if c["id"] in state["handled"] or c.get("username") == my_username:
                continue
            ts = datetime.fromisoformat(c["timestamp"].replace("+0000", "+00:00"))
            if datetime.now(timezone.utc) - ts > timedelta(days=6):
                state["handled"][c["id"]] = "too old"
                continue
            if not matches(c.get("text", ""), keywords):
                state["handled"][c["id"]] = "no keyword"
                continue
            result = {"comment_id": c["id"], "username": c.get("username", ""), "media_id": p["media_id"]}
            name = c.get("username") or "there"
            dm = (p.get("dm_template") or cfg["dm_template"]).format(name=name, title=p["title"], price=p["price"],
                                                                    link=p["link"])
            try:
                _req("POST", "me/messages", recipient=json.dumps({"comment_id": c["id"]}),
                     message=json.dumps({"text": dm}))
                result["dm"] = "sent"
            except InstagramError as e:
                result["dm"] = f"failed: {e}"
            try:
                reply = random.choice(cfg["public_replies"])
                _req("POST", f"{c['id']}/replies", message=f"@{name} {reply}" if c.get("username") else reply)
                result["reply"] = "sent"
            except InstagramError as e:
                result["reply"] = f"failed: {e}"
            state["handled"][c["id"]] = f"{result['dm']} / {result['reply']}"
            done.append(result)
            time.sleep(random.uniform(3, 8))  # don't fire replies in a burst
    _save_state(state)
    return done
