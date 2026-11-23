"""Carousel images made in the Gemini app (gemini.google.com) by driving Chrome, with safe limits.

The Gemini app is meant for a person, so this behaves like one and never works around Google's checks:
  - a visible Chrome window with its own profile (data/gemini_chrome/); you sign in once yourself:
        .venv\\Scripts\\python -m pipeline.gemini_web login
  - started by hand only (/gemini in the bot), at most PER_RUN images per run and PER_DAY per day,
    a random 40-90 s pause between prompts
  - stops at the first sign of trouble (sign-in page, "unusual traffic"/robot check, usage limit,
    two slides in a row without an image) and then stays off until the next day.
Each slide = a new chat: attach the product photo, pick "Create image", paste the prompt, wait,
save the full-size image (the "Download full-sized image" button).
"""
import base64
import json
import logging
import random
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger(__name__)

PROFILE_DIR = DATA_DIR / "gemini_chrome"
STATE_FILE = DATA_DIR / "gemini_web.json"
APP_URL = "https://gemini.google.com/app"
PER_RUN = 5                  # images per run
PER_DAY = 20                 # images per day, all runs together
PAUSE = (40, 90)             # seconds between prompts
IMAGE_TIMEOUT = 240          # seconds to wait for one image
TROUBLE = re.compile(r"unusual traffic|not a robot|verify it.s you|captcha|automated queries", re.I)
LIMIT = re.compile(r"limit|try again (later|tomorrow)|can.t (create|generate) (more|any)|too many", re.I)


class GeminiStop(Exception):
    """Something that needs a person: the run stops and no more runs start today."""


# --- limits ---------------------------------------------------------------------------------------
def _state() -> dict:
    s = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    if s.get("day") != date.today().isoformat():
        s = {"day": date.today().isoformat(), "count": 0, "blocked_until": s.get("blocked_until", "")}
    return s


def _save(s: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=1), encoding="utf-8")


def allowance() -> tuple[int, str]:
    """(images this run may make, reason when 0)."""
    s = _state()
    if s.get("blocked_until") and datetime.now().isoformat() < s["blocked_until"]:
        return 0, f"paused after a Google check/limit until {s['blocked_until'][:16].replace('T', ' ')}"
    left = PER_DAY - s["count"]
    if left <= 0:
        return 0, f"today's limit of {PER_DAY} images is used up"
    return min(PER_RUN, left), ""


def _count_one() -> None:
    s = _state()
    s["count"] += 1
    _save(s)


def _block(reason: str) -> None:
    s = _state()
    tomorrow = datetime.combine(date.today() + timedelta(days=1), datetime.min.time())
    s["blocked_until"], s["block_reason"] = tomorrow.isoformat(), reason
    _save(s)


# --- browser --------------------------------------------------------------------------------------
def _browser(pw):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(str(PROFILE_DIR), channel="chrome", headless=False,
                                                 accept_downloads=True, viewport={"width": 1280, "height": 900})


def _check(page) -> None:
    """Raise GeminiStop on a sign-in page or a robot check."""
    url = page.url
    if "accounts.google.com" in url:
        raise GeminiStop("Google wants you to sign in: run  .venv\\Scripts\\python -m pipeline.gemini_web login")
    if "/sorry/" in url or page.locator("iframe[src*='recaptcha']").count():
        raise GeminiStop("Google showed a robot check")
    try:
        body = page.locator("body").inner_text(timeout=2000)
    except Exception:
        return
    if TROUBLE.search(body[-3000:]):
        raise GeminiStop("Google showed an 'unusual traffic' / verification message")


def _open_menu(page) -> None:
    page.get_by_role("button", name=re.compile(r"upload (and|&) tools|add files", re.I)).first.click()
    page.wait_for_timeout(700)


def _new_chat(page) -> None:
    page.goto(APP_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    _check(page)
    editor = page.locator("rich-textarea .ql-editor")
    try:
        editor.wait_for(timeout=30000)
    except Exception:
        _check(page)
        raise GeminiStop("the Gemini page didn't load (signed out, or Google changed the page)")
    if page.locator("a[href*='ServiceLogin'], a[href*='accounts.google.com/v3/signin']").count() \
            or page.get_by_role("button", name=re.compile(r"^sign in$", re.I)).count():
        raise GeminiStop("Chrome isn't signed in to Google: run  .venv\\Scripts\\python -m pipeline.gemini_web login")


def _attach(page, photo: Path) -> None:
    _open_menu(page)
    with page.expect_file_chooser(timeout=15000) as fc:
        page.locator("[data-test-id='local-images-files-uploader-button']").click()
    fc.value.set_files(str(photo))
    # wait for the thumbnail in the input box and for the upload to finish
    page.locator("input-area-v2 img, input-container img").first.wait_for(timeout=60000)
    page.wait_for_timeout(2500)


def _pick_create_image(page) -> None:
    """Choose the 'Create image' tool (skipped quietly if the menu changed: the prompt asks for an image)."""
    try:
        _open_menu(page)
        page.locator(".cdk-overlay-container button", has_text=re.compile(r"create image", re.I)).first.click(timeout=5000)
        page.wait_for_timeout(800)
    except Exception:
        page.keyboard.press("Escape")
        log.info("Create image tool not found; sending the prompt as is")


def _send(page, prompt: str) -> None:
    editor = page.locator("rich-textarea .ql-editor")
    editor.click()
    page.keyboard.insert_text(" ".join(prompt.split()))
    page.wait_for_timeout(random.uniform(800, 1800))
    page.keyboard.press("Enter")


def _wait_image(page, n_before: int):
    """The generated image of the new answer; None if Gemini answered without one."""
    t0 = time.time()
    while time.time() - t0 < IMAGE_TIMEOUT:
        page.wait_for_timeout(5000)
        _check(page)
        responses = page.locator("model-response")
        if responses.count() <= n_before:
            continue
        last = responses.last
        img = last.locator("generated-image img.loaded, single-image img.loaded")
        if img.count():
            return last
        done = last.locator("message-actions").count() and last.locator("button[aria-label='Good response']").count()
        if done and time.time() - t0 > 20:
            text = last.inner_text()
            if LIMIT.search(text):
                raise GeminiStop(f"Gemini's usage limit: {text.strip()[:160]}")
            log.warning("Gemini answered without an image: %s", text.strip()[:200])
            return None
    return None


IMAGE = "generated-image img.loaded, single-image img.loaded"
COPY_JS = """el => { const c = document.createElement('canvas'); c.width = el.naturalWidth; c.height = el.naturalHeight;
    c.getContext('2d').drawImage(el, 0, 0); return c.toDataURL('image/png'); }"""


def _live_page(ctx, page):
    """The page to keep working in (a download can close the tab it started in)."""
    if page is not None and not page.is_closed():
        return page
    return next((p for p in ctx.pages if not p.is_closed()), None) or ctx.new_page()


def _settle(page) -> str:
    """Wait until the new chat has its own URL (/app/<id>); returns it."""
    for _ in range(30):
        if re.search(r"/app/[0-9a-f]{6,}", page.url):
            break
        page.wait_for_timeout(500)
    page.wait_for_timeout(3000)
    return page.url


def _copy_shown(page, out: Path) -> Path | None:
    """The image as shown on the page (a bit smaller than full size), copied without a download."""
    try:
        data = page.locator("model-response").last.locator(IMAGE).first.evaluate(COPY_JS)
        out.write_bytes(base64.b64decode(data.split(",", 1)[1]))
        return out
    except Exception as e:
        log.warning("copying the shown image failed: %s", str(e)[:200])
        return None


def _download_full(page, out: Path) -> Path:
    img = page.locator("model-response").last.locator(IMAGE).first
    img.hover()
    page.wait_for_timeout(800)
    with page.expect_download(timeout=90000) as dl:
        page.locator("model-response").last.locator("button[aria-label^='Download full']").first.click()
    dl.value.save_as(str(out))      # "Download full size image" / "full-sized"
    return out


def generate(photo: Path, prompts: list[str], out_dir: Path, progress=None) -> dict:
    """Make up to allowance() images, one prompt each. Returns {files, stopped, failed}.
    progress(i, n, file_or_None) is called after every slide."""
    from playwright.sync_api import sync_playwright
    n, why = allowance()
    if not n:
        return {"files": [], "stopped": why, "failed": 0}
    prompts = prompts[:n]
    out_dir.mkdir(parents=True, exist_ok=True)
    files, failed, misses, stopped = [], 0, 0, ""
    with sync_playwright() as pw:
        ctx = _browser(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            for i, prompt in enumerate(prompts, 1):
                if i > 1:
                    time.sleep(random.uniform(*PAUSE))
                f = None
                page = _live_page(ctx, page)
                try:
                    _new_chat(page)
                    _attach(page, photo)
                    _pick_create_image(page)
                    before = page.locator("model-response").count()
                    _send(page, prompt)
                    _count_one()
                    resp = _wait_image(page, before)
                    if resp is not None:
                        stem = out_dir / f"gemini-{datetime.now():%H%M%S}-{i}"
                        chat_url = _settle(page)
                        f = _copy_shown(page, stem.with_name(stem.name + "-shown.png"))
                        # Downloading right after generating can close Chrome: then reopen the chat
                        # (in a new browser if needed) and download from there. The copy is the fallback.
                        for attempt in range(2):
                            try:
                                if attempt:
                                    try:
                                        page = ctx.new_page()
                                    except Exception:
                                        ctx = _browser(pw)
                                        page = _live_page(ctx, None)
                                    page.goto(chat_url, wait_until="domcontentloaded")
                                    page.wait_for_timeout(8000)
                                f = _download_full(page, stem.with_suffix(".png"))
                                break
                            except Exception as e:
                                log.warning("full-size download failed (try %s): %s", attempt + 1, str(e)[:150])
                except GeminiStop:
                    raise
                except Exception as e:
                    log.warning("slide %s failed: %s", i, e)
                if f:
                    files.append(f)
                    misses = 0
                else:
                    failed += 1
                    misses += 1
                if progress:
                    progress(i, len(prompts), f)
                if misses >= 2:
                    raise GeminiStop("two slides in a row came back without an image")
        except GeminiStop as e:
            stopped = str(e)
            _block(stopped)
            log.warning("Gemini web stopped: %s", e)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return {"files": files, "stopped": stopped, "failed": failed}


CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def login() -> None:
    """Open plain Chrome (not driven by Playwright: Google refuses sign-in in automated browsers) on this
    profile so you can sign in by hand; runs later reuse the session. Close the window when done."""
    import subprocess
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print("Sign in to Google in the Chrome window, check Gemini opens, then close the window.")
    subprocess.run([CHROME, f"--user-data-dir={PROFILE_DIR}", "--no-first-run", APP_URL])


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["login"]:
        login()
    elif sys.argv[1:] == ["unblock"]:
        s = _state()
        s["blocked_until"] = ""
        _save(s)
        print("cleared")
    else:
        print("usage: python -m pipeline.gemini_web login | unblock")
