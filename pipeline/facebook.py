"""Facebook Page (Graph API): multi-photo posts and comment automation (public reply + private-reply
Messenger DM with the product link), mirroring instagram.py.

The Page token comes from data/fb_token.json (written by `python fb_token.py`) or FB_PAGE_TOKEN in
.env. A Page token derived from a long-lived user token does not expire.
Permissions: pages_show_list, pages_read_engagement, pages_read_user_content, pages_manage_posts,
pages_manage_engagement, pages_messaging.
"""
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone

import requests

from .config import DATA_DIR, env
from .instagram import matches

log = logging.getLogger(__name__)

API = "https://graph.facebook.com/v23.0"
TOKEN_FILE = DATA_DIR / "fb_token.json"
COMMENTS_FILE = DATA_DIR / "fb_comments.json"


class FacebookError(RuntimeError):
    pass


def _creds() -> dict:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    return {"page_token": env("FB_PAGE_TOKEN", required=False), "page_id": env("FB_PAGE_ID", required=False)}


def configured() -> bool:
    return bool(_creds().get("page_token"))


def page_id() -> str:
    pid = _creds().get("page_id")
    return pid or _req("GET", "me", fields="id")["id"]


def _req(method: str, path: str, **params) -> dict:
    params["access_token"] = _creds()["page_token"]
    for attempt in range(3):
        try:
            r = requests.request(method, f"{API}/{path.lstrip('/')}",
                                 **({"params": params} if method == "GET" else {"data": params}), timeout=60)
        except requests.RequestException as e:
            if attempt == 2:
                raise FacebookError(str(e)) from e
            time.sleep(5)
            continue
        data = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            if err.get("is_transient") and attempt < 2:
                time.sleep(10)
                continue
            raise FacebookError(f"{path}: {err.get('message') or r.text[:300]}")
        return data
    raise FacebookError(f"{path}: failed after retries")


# --- publishing ---------------------------------------------------------------------------------
def publish_photos(image_urls: list[str], message: str) -> dict:
    """One Page post with all images (uploaded unpublished, then attached to a feed post)."""
    pid = page_id()
    if len(image_urls) == 1:
        res = _req("POST", f"{pid}/photos", url=image_urls[0], message=message)
        post_id = res.get("post_id") or res["id"]
    else:
        media = [_req("POST", f"{pid}/photos", url=u, published="false")["id"] for u in image_urls]
        params = {f"attached_media[{i}]": json.dumps({"media_fbid": m}) for i, m in enumerate(media)}
        post_id = _req("POST", f"{pid}/feed", message=message, **params)["id"]
    info = _req("GET", post_id, fields="permalink_url,created_time")
    return {"post_id": post_id, "permalink": info.get("permalink_url", ""), "timestamp": info.get("created_time", "")}


def publish_reel(video_url: str, description: str) -> dict:
    """Page Reel from a public mp4 URL: start session -> upload by URL (rupload) -> finish/publish.
    The returned post_id is the video id (its comments are read like a post's)."""
    pid = page_id()
    start = _req("POST", f"{pid}/video_reels", upload_phase="start")
    vid = start["video_id"]
    r = requests.post(start.get("upload_url") or f"https://rupload.facebook.com/video-upload/v23.0/{vid}",
                      headers={"Authorization": f"OAuth {_creds()['page_token']}", "file_url": video_url}, timeout=300)
    if r.status_code >= 400 or not (r.json() if r.content else {}).get("success"):
        raise FacebookError(f"reel upload: {r.text[:300]}")
    _req("POST", f"{pid}/video_reels", upload_phase="finish", video_id=vid, video_state="PUBLISHED",
         description=description)
    deadline = time.time() + 600
    info = {}
    while time.time() < deadline:   # wait until processed so the permalink exists
        info = _req("GET", vid, fields="permalink_url,status")
        st = (info.get("status") or {}).get("video_status")
        if st in ("ready", "error"):
            break
        time.sleep(10)
    if (info.get("status") or {}).get("video_status") == "error":
        raise FacebookError(f"reel {vid} processing failed: {info.get('status')}")
    link = info.get("permalink_url", "")
    return {"post_id": vid, "permalink": f"https://www.facebook.com{link}" if link.startswith("/") else link}


# --- comment automation -------------------------------------------------------------------------
def _load_state() -> dict:
    if COMMENTS_FILE.exists():
        return json.loads(COMMENTS_FILE.read_text(encoding="utf-8"))
    return {"handled": {}}


def _save_state(state: dict) -> None:
    COMMENTS_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def handle_comments(posts: list[dict], cfg: dict) -> list[dict]:
    """posts: [{post_id, title, price, link}] for recent Page posts. Same rules as Instagram:
    keyword match -> private reply (Messenger, allowed within 7 days) + public reply."""
    state = _load_state()
    done = []
    keywords = cfg.get("keywords") or ["link"]
    pid = page_id()
    for p in posts:
        try:
            comments = _req("GET", f"{p['post_id']}/comments", fields="id,message,from,created_time",
                            filter="stream", order="reverse_chronological", limit=100).get("data", [])
        except FacebookError as e:
            log.warning("comments for %s: %s", p["post_id"], e)
            continue
        for c in comments:
            frm = c.get("from") or {}
            if c["id"] in state["handled"] or frm.get("id") == pid:
                continue
            ts = datetime.fromisoformat(c["created_time"].replace("+0000", "+00:00"))
            if datetime.now(timezone.utc) - ts > timedelta(days=6):
                state["handled"][c["id"]] = "too old"
                continue
            if not matches(c.get("message", ""), keywords):
                state["handled"][c["id"]] = "no keyword"
                continue
            # "from" is only returned for people who allowed it; fall back to a neutral greeting
            name = (frm.get("name") or "").split(" ")[0] or "there"
            result = {"comment_id": c["id"], "username": frm.get("name") or "someone", "post_id": p["post_id"]}
            dm = (p.get("dm_template") or cfg["dm_template"]).format(name=name, title=p["title"], price=p["price"],
                                                                    link=p["link"])
            try:
                _req("POST", f"{pid}/messages", recipient=json.dumps({"comment_id": c["id"]}),
                     message=json.dumps({"text": dm}), messaging_type="RESPONSE")
                result["dm"] = "sent"
            except FacebookError as e:
                result["dm"] = f"failed: {e}"
            try:
                _req("POST", f"{c['id']}/comments", message=random.choice(cfg["public_replies"]))
                result["reply"] = "sent"
            except FacebookError as e:
                result["reply"] = f"failed: {e}"
            state["handled"][c["id"]] = f"{result['dm']} / {result['reply']}"
            done.append(result)
            time.sleep(random.uniform(3, 8))
    _save_state(state)
    return done
