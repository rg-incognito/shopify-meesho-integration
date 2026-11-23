"""Scrapes Meesho product pages through a real (minimized) Chrome window.

Meesho sits behind Akamai bot protection, which blocks Playwright-launched and
headless browsers. Launching normal Chrome ourselves and attaching over CDP,
then visiting the homepage before product pages, gets through reliably.
"""
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from .config import CFG, ROOT

log = logging.getLogger(__name__)

HOME = "https://www.meesho.com/"
PRODUCT_ID_RE = re.compile(r"/p/([a-z0-9]+)", re.I)
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class ScrapeError(Exception):
    pass


class MeeshoBlocked(ScrapeError):
    pass


class ProductGone(ScrapeError):
    pass


def product_id_from_url(url: str) -> str | None:
    m = PRODUCT_ID_RE.search(url)
    return m.group(1).lower() if m else None


def full_res(url: str) -> str:
    # images.meesho.com/.../3y6yv_512.jpg -> 3y6yv.jpg (original upload)
    return re.sub(r"_\d+(\.\w+)$", r"\1", url)


def parse_product(next_data: dict, final_url: str) -> dict:
    try:
        d = next_data["props"]["pageProps"]["initialState"]["product"]["details"]["data"]
    except (KeyError, TypeError):
        d = None
    if not d or not d.get("name"):
        raise ScrapeError("No product data on page (layout may have changed)")

    supplier = (d.get("suppliers") or [{}])[0]
    base_price = d.get("price") or supplier.get("price")
    variants = []
    for inv in supplier.get("inventory") or []:
        v = inv.get("variation") or {}
        variants.append({
            "name": v.get("name") or "Default",
            "price": v.get("final_price") or base_price,
            "in_stock": bool(inv.get("in_stock")),
        })
    if not variants:
        variants = [{"name": n, "price": base_price, "in_stock": bool(d.get("in_stock"))}
                    for n in (d.get("variations") or ["Default"])]

    attributes = {}
    for section in (d.get("product_details") or {}).values():
        if isinstance(section, dict):
            for a in section.get("attributes") or []:
                if a.get("display_name") and a.get("value"):
                    attributes[a["display_name"]] = a["value"]

    catalog = d.get("catalog") or {}
    url = (d.get("meta_info") or {}).get("canonical_url") or final_url.split("?")[0]
    return {
        "meesho_id": product_id_from_url(url) or product_id_from_url(final_url),
        "url": url,
        "title": d["name"],
        "description": d.get("description") or "",
        "price": base_price,
        "mrp": (d.get("mrp_details") or {}).get("mrp"),
        "shipping_charges": (d.get("shipping") or {}).get("charges") or 0,
        "valid": bool(d.get("valid", True)),
        "in_stock": bool(d.get("in_stock")) and bool(d.get("valid", True)),
        "variants": variants,
        "images": [full_res(u) for u in d.get("images") or []],
        "attributes": attributes,
        "category": catalog.get("sub_sub_category_name") or "",
        "supplier": d.get("supplier_name") or supplier.get("name") or "",
        "rating": supplier.get("average_rating"),
        "rating_count": supplier.get("rating_count"),
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


class MeeshoScraper:
    """Not thread-safe: use from a single thread (the pipeline worker)."""

    def __init__(self):
        c = CFG["scraper"]
        self.chrome_path = c["chrome_path"]
        # env overrides let a bulk job run its own Chrome next to the bot's (see catalog.py)
        self.port = int(os.environ.get("SCRAPER_PORT") or c["debug_port"])
        self.profile = (ROOT / (os.environ.get("SCRAPER_PROFILE") or c["profile_dir"])).resolve()
        self._pw = None
        self._browser = None
        self._warmed = False

    # --- browser lifecycle -------------------------------------------------
    def _port_open(self) -> bool:
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def _ensure_chrome(self):
        if self._port_open():
            return
        log.info("Starting Chrome (minimized) on port %s", self.port)
        subprocess.Popen([
            self.chrome_path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            "--start-minimized",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ])
        for _ in range(40):
            if self._port_open():
                time.sleep(1)
                return
            time.sleep(0.5)
        raise ScrapeError(f"Chrome did not start (is chrome_path correct? {self.chrome_path})")

    def _connect(self):
        self._ensure_chrome()
        if self._browser and self._browser.is_connected():
            return
        if not self._pw:
            self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
        self._warmed = False

    def _reset_profile(self):
        """Akamai sometimes flags a profile; a fresh one clears it."""
        log.warning("Resetting Chrome profile after repeated blocks")
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        self._browser = None
        for _ in range(20):
            if not self._port_open():
                break
            time.sleep(0.5)
        # make sure no Chrome process from this profile survives (a lingering one keeps the flagged session)
        if sys.platform == "win32":
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | Where-Object { "
                            f"$_.CommandLine -like '*remote-debugging-port={self.port}*' }} | "
                            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                           capture_output=True, timeout=30)
        time.sleep(2)
        shutil.rmtree(self.profile, ignore_errors=True)
        self._connect()

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._browser = self._pw = None

    # --- scraping ----------------------------------------------------------
    def _load(self, url: str) -> tuple[int, str, str]:
        page = self._browser.contexts[0].new_page()
        try:
            if not self._warmed:
                page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                page.mouse.wheel(0, 600)
                page.wait_for_timeout(1000)
                self._warmed = True
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            return (resp.status if resp else 0), page.content(), page.url
        finally:
            page.close()

    def scrape(self, url: str) -> dict:
        self._connect()
        for attempt in range(3):
            if attempt == 2:
                self._reset_profile()
            status, html, final_url = self._load(url)
            if status == 404:
                raise ProductGone(f"Product page not found: {url}")
            if status == 403 or "Access Denied" in html[:3000]:
                log.warning("Meesho blocked attempt %s for %s", attempt + 1, url)
                self._warmed = False
                time.sleep(5 + attempt * 5)
                continue
            m = NEXT_DATA_RE.search(html)
            if not m:
                raise ScrapeError(f"Page has no product data: {final_url}")
            if not product_id_from_url(final_url):
                raise ScrapeError(f"Link did not open a Meesho product page: {final_url}")
            product = parse_product(json.loads(m.group(1)), final_url)
            if not product["valid"]:
                raise ProductGone(f"Product no longer available: {url}")
            return product
        raise MeeshoBlocked("Meesho blocked the browser 3 times. Try again in a few minutes.")


def download_images(urls: list[str], out_dir: Path) -> list[Path]:
    """Downloads Meesho photos. Meesho prints the supplier catalog code ("S-123456789") along the
    bottom edge, so a strip is trimmed off (scraper.crop_bottom_percent) before the photo is used."""
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    crop = float(CFG["scraper"].get("crop_bottom_percent", 0) or 0)
    paths = []
    for i, u in enumerate(urls, 1):
        path = out_dir / f"{i:02d}{Path(u).suffix or '.jpg'}"
        if not path.exists():
            r = requests.get(u, timeout=60)
            r.raise_for_status()
            path.write_bytes(r.content)
            if crop:
                with Image.open(path) as im:
                    w, h = im.size
                    trimmed = im.convert("RGB").crop((0, 0, w, h - round(h * crop / 100)))
                trimmed.save(path, quality=92)
        paths.append(path)
    return paths
