"""Bulk-add a curated shortlist from discover.py to Shopify as DRAFT products.

  python bulk_add.py select      -> data/discovery/navratri_selected.json (100 picks)
  python bulk_add.py run         -> scrape + AI copy + price + push each pick (resumable)

Each product is pushed as Draft, tagged `navratri` (joins the Navratri Special collection),
and added to its category collection plus the default collections (New Arrivals).
"""
import json
import logging
import random
import re
import sys
import time

from pipeline.config import DATA_DIR, setup_logging

DISCOVERY = DATA_DIR / "discovery" / "navratri.json"
SELECTED = DATA_DIR / "discovery" / "navratri_selected.json"

QUOTA = {"women": 31, "kids": 38, "jewellery": 21, "dupatta": 10}
COLLECTION = {"women": "Women's Ethnic Wear", "dupatta": "Women's Ethnic Wear",
              "kids": "Kids Ethnic Wear", "jewellery": "Jewellery"}
TAGS = ["navratri"]

# Reviewed by hand: not Navratri wear, or the wrong audience for the group
EXCLUDE_NAME = re.compile(r"^(kurta sets|sreeji kurtis|kashvi pretty kurtis|top & bottom set)$|daily wear kur|"
                          r"georgette embroidered mirror work long sleeveless kurti", re.I)
MOVE_TO_WOMEN = re.compile(r"women lehenga choli full embroidery", re.I)
JEWELLERY_OK = re.compile(r"oxid|kamar|choker|navratri|garba|jhumk|tribal|kada|bangle|necklace|set|earring|anklet|payal", re.I)

log = logging.getLogger("bulk")


def norm(name: str) -> str:
    return re.sub(r"[^a-z]+", " ", (name or "").lower()).strip()


def select():
    from pipeline.sheet import Sheet
    existing = {r["meesho_id"] for r in Sheet().all()}
    d = json.loads(DISCOVERY.read_text(encoding="utf-8"))
    for item in list(d.get("kids", [])):
        if MOVE_TO_WOMEN.search(item["name"] or ""):
            d["kids"].remove(item)
            d.setdefault("women", []).append({**item, "group": "women"})
    picks = []
    for group, n in QUOTA.items():
        names_seen, chosen = set(), []
        for item in sorted(d.get(group, []), key=lambda x: -x["score"]):
            key = (norm(item["name"]), item["price"]) if len(norm(item["name"])) > 25 else (norm(item["name"]),)
            if (item["product_id"] in existing or EXCLUDE_NAME.search((item["name"] or "").strip())
                    or key in names_seen
                    or (group == "jewellery" and not JEWELLERY_OK.search(item["name"] or ""))):
                continue
            names_seen.add(key)
            chosen.append(item)
            if len(chosen) == n:
                break
        print(f"{group:<10} picked {len(chosen)}/{n}")
        picks += chosen
    SELECTED.write_text(json.dumps(picks, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(picks)} picks -> {SELECTED}")


def run():
    from pipeline.workflow import Pipeline
    picks = json.loads(SELECTED.read_text(encoding="utf-8"))
    p = Pipeline()
    done = {r["meesho_id"]: r["status"] for r in p.sheet.all()}
    ok = failed = skipped = 0
    try:
        for i, item in enumerate(picks, 1):
            mid = item["product_id"]
            if done.get(mid) == "PUSHED":
                skipped += 1
                continue
            t0 = time.time()
            try:
                if done.get(mid) not in ("READY", "APPROVED") or not p.ai_images(mid) and not (p.data_dir(mid) / "copy.json").exists():
                    p.process(item["url"], "bulk-navratri")
                p.sheet.upsert(mid, collections=COLLECTION[item["group"]])
                r = p.push(mid)  # direct push (status stays Draft on Shopify)
                p.shopify.gql("""mutation($id: ID!, $tags: [String!]!) { tagsAdd(id: $id, tags: $tags) {
                    userErrors { message } } }""", {"id": r["product_id"], "tags": TAGS})
                ok += 1
                log.info("[%d/%d] OK %s %s (%.0fs)", i, len(picks), mid, item["group"], time.time() - t0)
                print(f"[{i}/{len(picks)}] OK   {mid} {item['group']:<9} {item['name'][:50]}", flush=True)
            except Exception as e:
                failed += 1
                log.exception("[%d/%d] FAIL %s", i, len(picks), mid)
                print(f"[{i}/{len(picks)}] FAIL {mid} {item['group']:<9} {str(e)[:120]}", flush=True)
                if "blocked" in str(e).lower():
                    print("Meesho is blocking; pausing 5 minutes", flush=True)
                    time.sleep(300)
            time.sleep(random.uniform(4, 9))
    finally:
        p.close()
    print(f"DONE ok={ok} failed={failed} skipped={skipped}", flush=True)


def restudio():
    """Give already-pushed products from the shortlist free studio photos and re-push them."""
    from pipeline.workflow import Pipeline
    picks = {i["product_id"] for i in json.loads(SELECTED.read_text(encoding="utf-8"))}
    p = Pipeline()
    try:
        rows = [r for r in p.sheet.by_status("PUSHED") if r["meesho_id"] in picks]
        for i, r in enumerate(rows, 1):
            mid = r["meesho_id"]
            if p.ai_images(mid) or p.studio_images(mid):
                continue
            try:
                n = p.make_studio(mid)
                p.push(mid)  # images changed -> re-uploaded, replacing the raw Meesho photos
                p.sheet.upsert(mid, notes="studio photos")
                print(f"[{i}/{len(rows)}] studio {mid}: {n} photos", flush=True)
            except Exception as e:
                print(f"[{i}/{len(rows)}] FAIL {mid}: {str(e)[:120]}", flush=True)
    finally:
        p.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    setup_logging("bulk")
    {"select": select, "run": run, "restudio": restudio}[sys.argv[1]]()
