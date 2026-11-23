"""Pipeline steps: process (scrape + AI + price) -> review -> push -> resync.

All methods are blocking and must be called from one thread at a time
(the Telegram bot runs them on a single worker thread).
"""
import hashlib
import html
import json
import logging
import random
import re
import time
from pathlib import Path

from .cleanup import clean_listing_image
from .config import CFG, DATA_DIR, IMAGES_DIR
from .pricing import price_label, price_variants
from .scraper import MeeshoBlocked, MeeshoScraper, ProductGone, ScrapeError, download_images, product_id_from_url
from .sheet import Sheet, now

log = logging.getLogger(__name__)


def _load(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _sig(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _sku(meesho_id: str, variant: str) -> str:
    return f"MSH-{meesho_id}-{re.sub(r'[^A-Za-z0-9]+', '', variant).upper() or 'STD'}"


def _num(value) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("₹", "").strip()) or None
    except ValueError:
        return None


class Pipeline:
    def __init__(self):
        self.sheet = Sheet()
        self._scraper = self._ai = self._shopify = None

    # lazily created so e.g. `push` doesn't start Chrome
    @property
    def scraper(self):
        self._scraper = self._scraper or MeeshoScraper()
        return self._scraper

    @property
    def ai(self):
        if not self._ai:
            from .ai import GeminiAI
            self._ai = GeminiAI()
        return self._ai

    @property
    def shopify(self):
        if not self._shopify:
            from .shopify import Shopify
            self._shopify = Shopify()
        return self._shopify

    def close(self):
        if self._scraper:
            self._scraper.close()

    # --- paths ---------------------------------------------------------------
    @staticmethod
    def data_dir(mid: str) -> Path:
        return DATA_DIR / mid

    @staticmethod
    def ai_images(mid: str) -> list[Path]:
        return sorted((IMAGES_DIR / mid / "ai").glob("*.jpg"))

    @staticmethod
    def studio_images(mid: str) -> list[Path]:
        return sorted((IMAGES_DIR / mid / "studio").glob("*.jpg"))

    @staticmethod
    def listing_images(mid: str) -> list[Path]:
        """AI images if generated, else the original Meesho photos. Studio cut-outs only when
        studio.use_in_listing is on (background removal damaged white/sheer fabric and dupattas)."""
        studio = Pipeline.studio_images(mid) if CFG.get("studio", {}).get("use_in_listing") else []
        return Pipeline.ai_images(mid) or studio or Pipeline.clean_images(mid)

    @staticmethod
    def clean_images(mid: str) -> list[Path]:
        """Original Meesho photos with the supplier code painted out and square photos padded to 4:5
        (built once into images/<id>/listing). Falls back to the raw originals if clean-up is off."""
        orig = sorted((IMAGES_DIR / mid / "orig").glob("*"))
        if not CFG.get("listing_cleanup", {}).get("enabled", True):
            return orig
        out_dir = IMAGES_DIR / mid / "listing"
        out = []
        for src in orig:
            dst = out_dir / f"{src.stem}.jpg"
            if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
                try:
                    clean_listing_image(src, dst, pad=CFG.get("listing_cleanup", {}).get("pad_to_portrait", True))
                except Exception as e:
                    log.warning("clean-up failed for %s: %s", src, e)
                    dst = src
            out.append(dst)
        return out

    def make_studio(self, mid: str) -> int:
        from .studio import make_studio_images
        orig = sorted((IMAGES_DIR / mid / "orig").glob("*"))
        return len(make_studio_images(orig, IMAGES_DIR / mid / "studio"))

    def _generate_images(self, product: dict, orig: list[Path]) -> tuple[int, str]:
        """Returns (number of AI images, warning). Falls back to studio or Meesho photos if allowed."""
        mid = product["meesho_id"]
        try:
            return len(self.ai.generate_images(product, orig, IMAGES_DIR / mid / "ai")), ""
        except Exception as e:
            if not CFG["gemini"].get("fallback_to_original_images", True):
                raise
            for old in (IMAGES_DIR / mid / "ai").glob("*.jpg"):
                old.unlink()
            reason = "Gemini image quota/billing not set up" if "RESOURCE_EXHAUSTED" in str(e) else str(e)[:150]
            if CFG["gemini"].get("fallback_images", "original") == "studio":
                try:
                    n = self.make_studio(mid)
                    log.info("AI images unavailable for %s; made %d studio photos", mid, n)
                    return 0, f"AI images unavailable ({reason}); using free studio photos"
                except Exception as se:
                    log.warning("Studio photos failed for %s: %s", mid, se)
            log.warning("AI images failed for %s, using Meesho photos: %s", mid, e)
            return 0, f"AI images failed ({reason}); using original Meesho photos"

    def product(self, mid: str) -> dict:
        p = _load(self.data_dir(mid) / "product.json")
        if not p:
            raise KeyError(f"No scraped data for {mid}; process the link first")
        return p

    # --- step 1: process a link ---------------------------------------------
    def process(self, url: str, added_by: str = "") -> dict:
        """Scrape, write copy, generate images and price a Meesho link. Returns a summary."""
        mid = product_id_from_url(url)
        try:
            product = self.scraper.scrape(url)
            mid = product["meesho_id"]
            existing = self.sheet.get(mid)
            fields = dict(
                status="PROCESSING", meesho_url=product["url"], meesho_title=product["title"],
                meesho_price=price_label([v["price"] for v in product["variants"]]),
                in_stock="yes" if product["in_stock"] else "no",
                sizes=", ".join(v["name"] for v in product["variants"]),
                category=product["category"], last_scraped=now(), error="",
                preview=f'=IMAGE("{product["images"][0]}")' if product["images"] else "",
            )
            if not existing:
                fields["added_by"] = added_by
            self.sheet.upsert(mid, **fields)
            _save(self.data_dir(mid) / "product.json", product)

            orig = download_images(product["images"], IMAGES_DIR / mid / "orig")
            copy = self.ai.write_copy(product)
            _save(self.data_dir(mid) / "copy.json", copy)
            n_ai, warning = self._generate_images(product, orig)

            override = _num(existing.get("price_override")) if existing else None
            variants = price_variants(product, override)
            status = "APPROVED" if CFG["pipeline"]["auto_approve"] else "READY"
            compare = [v["compare_at"] for v in variants if v["compare_at"]]
            self.sheet.upsert(
                mid, status=status, shopify_title=copy["title"],
                my_price=price_label([v["my_price"] for v in variants]),
                compare_at=price_label(compare) if compare else "",
                ai_images=n_ai, notes=warning,
            )
            return self.summary(mid)
        except Exception as e:
            log.exception("Processing failed for %s", url)
            if mid:
                self.sheet.upsert(mid, status="ERROR", error=str(e)[:500], meesho_url=url)
            raise

    def regenerate_images(self, mid: str) -> dict:
        product = self.product(mid)
        orig = sorted((IMAGES_DIR / mid / "orig").glob("*"))
        n_ai, warning = self._generate_images(product, orig)
        row = self.sheet.get(mid) or {}
        status = "APPROVED" if row.get("status") == "PUSHED" else "READY"
        self.sheet.upsert(mid, status=status, ai_images=n_ai, notes=warning, error="")
        return self.summary(mid)

    def summary(self, mid: str) -> dict:
        product = self.product(mid)
        copy = _load(self.data_dir(mid) / "copy.json", {})
        row = self.sheet.get(mid) or {}
        variants = price_variants(product, _num(row.get("price_override")))
        return {
            "meesho_id": mid,
            "status": row.get("status"),
            "title": copy.get("title") or product["title"],
            "meesho_price": price_label([v["price"] for v in variants]),
            "my_price": price_label([v["my_price"] for v in variants]),
            "compare_at": max((v["compare_at"] or 0) for v in variants) or None,
            "sizes": [v["name"] for v in variants],
            "in_stock": product["in_stock"],
            "images": [str(p) for p in self.listing_images(mid)],
            "ai_images": bool(self.ai_images(mid)),
            "warning": row.get("notes") or "",
            "url": product["url"],
        }

    # --- step 2: review ------------------------------------------------------
    def set_status(self, mid: str, status: str) -> None:
        self.sheet.upsert(mid, status=status)

    def set_price(self, mid: str, price: float | None) -> dict:
        self.sheet.upsert(mid, price_override=int(price) if price else "")
        variants = price_variants(self.product(mid), price)
        self.sheet.upsert(mid, my_price=price_label([v["my_price"] for v in variants]))
        return self.summary(mid)

    # --- step 3: push to Shopify ---------------------------------------------
    def _build_input(self, mid: str, product: dict, copy: dict, variants: list[dict], state: dict,
                     new: bool, include_copy: bool) -> dict:
        option = CFG["shopify"]["option_name"]
        known = state.get("variants", {})
        stock = int(CFG["shopify"]["stock_when_available"])
        loc = self.shopify.location_id() if new else None
        var_inputs = []
        for i, v in enumerate(variants, 1):
            sku = _sku(mid, v["name"])
            vi = {
                "optionValues": [{"optionName": option, "name": v["name"]}],
                "position": i,
                "price": str(v["my_price"]),
                "compareAtPrice": str(v["compare_at"]) if v["compare_at"] else None,
                "sku": sku,
                "inventoryPolicy": "DENY",
                "inventoryItem": {"tracked": True, "cost": str(v["cost"]), "requiresShipping": True},
            }
            if sku in known:
                vi["id"] = known[sku]["id"]
            if new:
                vi["inventoryQuantities"] = [{"locationId": loc, "name": "available",
                                              "quantity": stock if v["in_stock"] else 0}]
            var_inputs.append(vi)

        data = {
            "productOptions": [{"name": option, "position": 1,
                                "values": [{"name": v["name"]} for v in variants]}],
            "variants": var_inputs,
        }
        if include_copy:
            fix = {"Ocassion": "Occasion", "Occassion": "Occasion"}  # Meesho's own typos
            specs = "".join(f"<li><strong>{html.escape(fix.get(k, k))}:</strong> {html.escape(str(val))}</li>"
                            for k, val in product["attributes"].items()
                            if k not in ("Net Quantity (N)", "Generic Name"))
            tags = {t.strip().lower() for t in copy.get("tags", []) if t.strip()}
            tags.update(CFG["store"].get("extra_tags") or [])
            if product["category"]:
                tags.add(product["category"].lower())
            data.update({
                "title": copy["title"],
                "descriptionHtml": copy["description_html"] + (f"<h4>Specifications</h4><ul>{specs}</ul>" if specs else ""),
                "vendor": CFG["store"]["vendor"],
                "productType": product["category"],
                "tags": sorted(tags),
                "seo": {"title": copy["seo_title"], "description": copy["seo_description"]},
            })
        if new:
            data["status"] = CFG["shopify"]["status_on_create"]
            data["metafields"] = [
                {"namespace": "meesho", "key": "product_id", "type": "single_line_text_field", "value": mid},
                {"namespace": "meesho", "key": "source_url", "type": "url", "value": product["url"]},
            ]
        return data

    def push(self, mid: str) -> dict:
        try:
            row = self.sheet.get(mid)
            if not row:
                raise KeyError(f"{mid} is not in the sheet")
            product = self.product(mid)
            copy = _load(self.data_dir(mid) / "copy.json")
            images = self.listing_images(mid)
            if not copy or not images:
                raise RuntimeError("Missing AI copy or images; reprocess this product")
            state_path = self.data_dir(mid) / "shopify.json"
            state = _load(state_path, {})

            pid = row.get("shopify_product_id") or None
            if pid and not self.shopify.product_exists(pid):
                log.warning("%s was deleted in Shopify; recreating", pid)
                pid, state = None, {}
            new = pid is None

            variants = price_variants(product, _num(row.get("price_override")))
            img_sig = _sig([(p.name, p.stat().st_size, p.stat().st_mtime) for p in images])
            copy_sig = _sig(copy)
            data = self._build_input(mid, product, copy, variants, state, new,
                                     include_copy=new or state.get("copy_sig") != copy_sig)
            if new or state.get("images_sig") != img_sig:
                data["files"] = [{"originalSource": self.shopify.upload_image(p), "contentType": "IMAGE",
                                  "alt": copy["title"]} for p in images]

            result = self.shopify.product_set(data, pid)
            pid = result["id"]
            state.update(
                product_id=pid, images_sig=img_sig, copy_sig=copy_sig,
                variants={n["sku"]: {"id": n["id"], "inventory_item_id": n["inventoryItem"]["id"]}
                          for n in result["variants"]["nodes"]},
            )
            _save(state_path, state)
            if not new:
                stock = int(CFG["shopify"]["stock_when_available"])
                self.shopify.set_inventory([
                    (state["variants"][_sku(mid, v["name"])]["inventory_item_id"], stock if v["in_stock"] else 0)
                    for v in variants if _sku(mid, v["name"]) in state["variants"]
                ])
            if new:
                self.shopify.publish(pid)  # visible on Online Store once status is ACTIVE
            self.sheet.upsert(mid, status="PUSHED", shopify_product_id=pid,
                              shopify_admin_url=self.shopify.admin_url(pid), last_pushed=now(), error="",
                              my_price=price_label([v["my_price"] for v in variants]),
                              shopify_status=result["status"])
            collections = self._apply_collections(mid, pid, row)
            return {"meesho_id": mid, "product_id": pid, "created": new, "collections": collections,
                    "admin_url": self.shopify.admin_url(pid), "status": result["status"]}
        except Exception as e:
            log.exception("Push failed for %s", mid)
            self.sheet.upsert(mid, status="ERROR", error=f"push: {e}"[:500])
            raise

    # --- collections & going live ---------------------------------------------
    @staticmethod
    def _titles(value) -> list[str]:
        return [t.strip() for t in str(value or "").split(",") if t.strip()]

    def _apply_collections(self, mid: str, pid: str, row: dict) -> list[str]:
        """Adds the product to default + chosen collections (creating any that don't exist)."""
        wanted = list(dict.fromkeys(CFG["shopify"].get("default_collections") or []))
        wanted += [t for t in self._titles(row.get("collections")) if t not in wanted]
        if not wanted:
            return []
        current = {c["title"].lower() for c in (self.shopify.product_info(pid) or {}).get("collections", [])}
        for title in wanted:
            if title.lower() not in current:
                self.shopify.add_to_collection(self.shopify.collection_by_title(title)["id"], pid)
        self.sheet.upsert(mid, collections=", ".join(wanted))
        return wanted

    def collection_choices(self, mid: str) -> tuple[list[dict], set[str]]:
        """(all manual collections, ids of the ones this product is in / will be added to)."""
        row = self.sheet.get(mid) or {}
        cols = self.shopify.manual_collections()
        pid = row.get("shopify_product_id")
        info = self.shopify.product_info(pid) if pid else None
        if info:
            selected = {c["id"] for c in info["collections"]}
        else:
            titles = {t.lower() for t in self._titles(row.get("collections"))}
            selected = {c["id"] for c in cols if c["title"].lower() in titles}
        return cols, selected

    def toggle_collection(self, mid: str, collection_id: str) -> tuple[str, bool]:
        """Adds/removes the product to/from a collection. Returns (title, now_member)."""
        cols, selected = self.collection_choices(mid)
        col = next((c for c in cols if c["id"] == collection_id), None)
        if not col:
            raise KeyError("Collection not found (it may have been deleted or made automatic)")
        return col["title"], self._set_membership(mid, col, collection_id not in selected)

    def add_collection_by_title(self, mid: str, title: str) -> str:
        """Adds the product to the collection with this title, creating it if needed."""
        col = self.shopify.collection_by_title(title)
        self._set_membership(mid, col, True)
        return col["title"]

    def _set_membership(self, mid: str, col: dict, member: bool) -> bool:
        row = self.sheet.get(mid) or {}
        titles = [t for t in self._titles(row.get("collections")) if t.lower() != col["title"].lower()]
        if member:
            titles.append(col["title"])
        pid = row.get("shopify_product_id")
        if pid and self.shopify.product_exists(pid):
            if member:
                self.shopify.add_to_collection(col["id"], pid)
            else:
                self.shopify.remove_from_collection(col["id"], pid)
        self.sheet.upsert(mid, collections=", ".join(titles))
        return member

    def set_live(self, mid: str, active: bool) -> dict:
        """Make a pushed product ACTIVE (visible on the Online Store) or back to DRAFT."""
        row = self.sheet.get(mid) or {}
        pid = row.get("shopify_product_id")
        if not pid:
            raise RuntimeError(f"{mid} is not on Shopify yet; approve it first")
        if not self.shopify.product_exists(pid):
            raise RuntimeError(f"{mid} was deleted in Shopify")
        status = "ACTIVE" if active else "DRAFT"
        self.shopify.set_status(pid, status)
        if active:  # (re)publish after activating so it's visible on the Online Store immediately
            self.shopify.publish(pid)
        self.sheet.upsert(mid, shopify_status=status)
        info = self.shopify.product_info(pid) or {}
        return {"meesho_id": mid, "status": status, "url": info.get("onlineStoreUrl"),
                "admin_url": self.shopify.admin_url(pid)}

    def push_approved(self) -> tuple[list[dict], list[str]]:
        done, failed = [], []
        for row in self.sheet.by_status("APPROVED"):
            try:
                done.append(self.push(row["meesho_id"]))
            except Exception as e:
                failed.append(f"{row['meesho_id']}: {e}")
        return done, failed

    # --- step 4: daily resync ------------------------------------------------
    def resync(self) -> list[str]:
        """Re-scrape every pushed product; update Shopify when Meesho price/stock changed."""
        changes = []
        for row in self.sheet.by_status("PUSHED"):
            msg = self._resync_one(row)
            if msg:
                changes.append(msg)
            time.sleep(random.uniform(3, 8))
        return changes

    def upgrade_copy(self, limit: int) -> dict:
        """Rewrites template listing text (written when the Gemini quota was used up) with AI and
        pushes it, up to `limit` products. Stops as soon as Gemini falls back to the template again
        (daily quota gone). Product URL handles are unchanged. Returns {"upgraded": [...], "left": n,
        "quota_out": bool}."""
        rows = [r for r in self.sheet.by_status("PUSHED")
                if _load(self.data_dir(str(r["meesho_id"])) / "copy.json", {}).get("_model") == "template"]
        out = {"upgraded": [], "left": len(rows), "quota_out": False}
        for row in rows[:limit]:
            mid = str(row["meesho_id"])
            copy = self.ai.write_copy(self.product(mid))
            if copy.get("_model") == "template":
                out["quota_out"] = True
                break
            _save(self.data_dir(mid) / "copy.json", copy)
            self.sheet.upsert(mid, shopify_title=copy["title"])
            try:
                self.push(mid)  # copy signature changed -> title, description, tags and SEO are updated
            except Exception as e:
                log.warning("copy upgrade push failed for %s: %s", mid, e)
                continue
            out["upgraded"].append(mid)
            out["left"] -= 1
        return out

    def resync_due(self, every_hours: float, limit: int) -> dict:
        """Rolling re-check: the `limit` pushed products checked longest ago (and at least
        `every_hours` ago). Called every few minutes so every product is checked ~every_hours
        without one long blocking run. Returns {"changes": [...], "checked": n, "errors": [...]}."""
        state_path = DATA_DIR / "resync_state.json"
        state = _load(state_path, {})
        cutoff = time.time() - every_hours * 3600
        rows = [r for r in self.sheet.by_status("PUSHED") if state.get(str(r["meesho_id"]), 0) < cutoff]
        rows.sort(key=lambda r: state.get(str(r["meesho_id"]), 0))
        out = {"changes": [], "checked": 0, "errors": [], "blocked": False}
        for row in rows[:limit]:
            mid = str(row["meesho_id"])
            try:
                msg = self._resync_one(row, raise_blocked=True)
            except MeeshoBlocked as e:
                out["blocked"] = True
                out["errors"].append(f"{mid}: Meesho blocked the scraper ({e})")
                break  # don't keep hitting Meesho; the rest wait for the next round
            state[mid] = time.time()  # attempted, even if it failed: retried next cycle, not every run
            out["checked"] += 1
            if msg and ("could not check" in msg or ": error (" in msg):
                out["errors"].append(msg)
            elif msg:
                out["changes"].append(msg)
            time.sleep(random.uniform(1, 3))
        _save(state_path, state)
        return out

    def _resync_one(self, row: dict, raise_blocked: bool = False) -> str | None:
        """Checks one product against Meesho; returns a message when something changed."""
        mid = row["meesho_id"]
        try:
            if not self.shopify.product_exists(row["shopify_product_id"]):
                self.sheet.upsert(mid, status="REJECTED", notes="Deleted in Shopify; no longer synced")
                return f"{mid}: deleted in Shopify, stopped syncing"
            old = self.product(mid)
            try:
                new = self.scraper.scrape(row["meesho_url"])
            except ProductGone:
                self.shopify.set_status(row["shopify_product_id"], "DRAFT")
                self.sheet.upsert(mid, status="UNAVAILABLE", last_scraped=now(),
                                  error="Removed from Meesho; set to Draft")
                return f"{mid}: removed from Meesho, set to Draft"

            before = {v["name"]: (v["price"], v["in_stock"]) for v in old["variants"]}
            after = {v["name"]: (v["price"], v["in_stock"]) for v in new["variants"]}
            _save(self.data_dir(mid) / "product.json", new)
            self.sheet.upsert(mid, last_scraped=now(), in_stock="yes" if new["in_stock"] else "no",
                              meesho_price=price_label([v["price"] for v in new["variants"]]))
            msgs = []
            if before != after:
                self.push(mid)
                diffs = []
                for name in sorted(set(before) | set(after)):
                    b, a = before.get(name), after.get(name)
                    if b != a:
                        diffs.append(f"{name}: {b and f'₹{b[0]}' + ('' if b[1] else ' OOS')} → "
                                     f"{a and f'₹{a[0]}' + ('' if a[1] else ' OOS')}")
                msgs.append(f"{mid}: " + "; ".join(diffs))
            override = _num(row.get("price_override"))
            if override and any(override <= v["price"] for v in new["variants"]):
                msgs.append(f"{mid}: ⚠️ your override ₹{int(override)} is now below Meesho cost")
            return "\n".join(msgs) or None
        except MeeshoBlocked:
            if raise_blocked:
                raise
            log.warning("Resync blocked for %s", mid)
            return f"{mid}: could not check (Meesho blocked the scraper)"
        except ScrapeError as e:
            log.warning("Resync scrape failed for %s: %s", mid, e)
            return f"{mid}: could not check ({e})"
        except Exception as e:
            log.exception("Resync failed for %s", mid)
            return f"{mid}: error ({e})"
