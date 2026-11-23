"""Grow the catalog with Meesho's winning products: up to N per category, pushed to Shopify as ACTIVE.

  python catalog.py discover        search Meesho -> data/discovery/catalog.json (ranked candidates)
  python catalog.py select [N]      top N per category not already in the store (default 300)
  python catalog.py run [limit]     scrape + copy + clean photos + price + push + Active (resumable)
  python catalog.py status          progress per category
  python catalog.py daily [N]       daily growth: search today's slice of queries, pick the top N new
                                    winners per category (default 100) and push them (resumable per day)

"Winning" = well rated by many buyers: rating >= MIN_RATING with >= MIN_COUNT ratings, valid on
Meesho, price in a sensible range, near-duplicates (same product from several sellers) removed,
ranked by rating x log10(1 + number of ratings).

Runs its own Chrome (port 9334, separate profile) so it never clashes with the bot's scraper.
"""
import json
import logging
import math
import os
import random
import re
import sys
import time

os.environ.setdefault("SCRAPER_PORT", "9334")
os.environ.setdefault("SCRAPER_PROFILE", ".chrome-profile-catalog")

from pipeline.config import DATA_DIR, setup_logging  # noqa: E402

CANDIDATES = DATA_DIR / "discovery" / "catalog.json"
SELECTED = DATA_DIR / "discovery" / "catalog_selected.json"
DAILY_DIR = DATA_DIR / "discovery" / "daily"

QUERIES = {
    "women": ["lehenga choli for women", "chaniya choli women", "kurta set with dupatta women", "anarkali kurta set",
              "saree for women", "silk saree", "georgette saree", "printed saree", "ethnic gown women",
              "sharara set women", "palazzo kurta set women", "embroidered kurta set", "cotton kurti women",
              "bandhani saree", "salwar suit women", "dupatta for women", "ethnic co ord set women",
              "straight kurta women", "lehenga choli wedding", "kurti with pant women"],
    "kids": ["girls lehenga choli", "kids ethnic gown girls", "girls ethnic frock", "baby girl lehenga choli",
             "girls sharara set", "girls anarkali dress", "boys kurta pyjama", "boys ethnic wear",
             "boys sherwani kids", "baby boy dhoti kurta", "kids chaniya choli", "girls pattu pavadai",
             "girls kurti set", "boys kurta set with jacket"],
    "jewellery": ["oxidised jewellery set", "oxidised earrings women", "jhumka earrings", "choker necklace set",
                  "kundan jewellery set", "american diamond necklace set", "bangles set women", "mangalsutra",
                  "anklet payal women", "kamarbandh waist chain", "nose ring nath", "maang tikka",
                  "temple jewellery set", "pearl jewellery set", "earrings for women party wear"],
    "nightwear": ["nighty for women", "cotton nighty women", "night suit for women", "pyjama set women",
                  "nightdress women", "maternity nighty feeding", "satin night suit women", "printed nighty",
                  "kaftan nightwear women", "shorts night suit women", "long nighty women"],
}
# second pass (python catalog.py discover more): merged into the same candidate file
EXTRA_QUERIES = {
    "women": ["kurti for women", "rayon kurti women", "printed kurta set women", "chikankari kurti",
              "festive saree women", "cotton saree women", "organza saree", "banarasi saree",
              "semi stitched lehenga choli", "dress material unstitched women", "gown for women party wear",
              "kurta palazzo dupatta set", "a line kurti women", "gharara set women", "chanderi kurta set"],
    "kids": ["girls gown party wear", "girls dress ethnic", "kids lehenga", "girls salwar suit",
             "kids kurta pyjama boys", "boys dhoti kurta set", "boys nehru jacket set", "baby girl ethnic dress",
             "girls choli set", "girls silk frock", "kids navratri dress", "girls kurta palazzo set",
             "kids festive wear", "boys ethnic kurta"],
    "jewellery": ["earrings combo women", "necklace set women", "gold plated jewellery set", "bridal jewellery set",
                  "bracelet women", "finger ring women", "oxidised bangles", "chandbali earrings", "hoop earrings",
                  "jewellery combo set women", "hair accessories bridal", "toe ring silver"],
    "nightwear": ["nightwear women", "night gown women", "cotton night suit women", "pajama set women cotton",
                  "capri set women nightwear", "kurti pajama set women", "lounge wear women", "night dress for women",
                  "sleepwear women", "loungewear set women", "printed night suit women cotton",
                  "track pant pajama women"],
}
PAGES = 10
MIN_RATING, MIN_COUNT = 4.0, 30   # collect broadly; ranking puts the most-proven first
PRICE_RANGE = {"women": (200, 2500), "kids": (150, 1500), "jewellery": (60, 1000), "nightwear": (150, 1200)}
COLLECTION = {"women": "Women's Ethnic Wear", "kids": "Kids Ethnic Wear", "jewellery": "Jewellery",
              "nightwear": "Nightwear"}
# Meesho category -> store group (decides where a product belongs, whatever search found it)
GROUP_BY_CATEGORY = {
    "women": {"Sarees", "Kurtis & Kurtas", "Kurti With Dupatta & Bottomwear", "Kurti With Bottomwear", "Lehengas",
              "Gowns", "Suits & Dress Materials", "Dupattas", "Ethnic Gowns"},
    "kids": {"Kurta Sets", "Lehenga Cholis", "Frocks & Dresses", "Ethnic Gowns", "Sherwanis", "Salwar Suit Sets",
             "Clothing Sets", "Boys Ethnic Pyjamas & Dhoti Pants", "Boys Suits", "Ethnic Jackets"},
    "jewellery": {"Jewellery Set", "Earrings & Studs", "Bangles & Bracelets", "Kamarband and Belly Chains",
                  "Anklets & Toe Rings", "Mangalsutras", "Rings", "Maangtika", "Hair Accessories", "Nosepins",
                  "Necklaces & Chains", "Gajra/Floral and other Hair Accessories", "Mathapatti"},
    "nightwear": {"Nightsuits", "Nightdress", "Pyjamas and Lounge Pants", "Feeding Nightwear", "Kaftans",
                  "Maternity Sleepwear", "Capris"},
}
NIGHT_WORDS = ("night", "sleep", "lounge", "pyjama", "pajama", "nighty", "kaftan")
MEN_WORDS = ("men's", " men ", "mens ", "for men", "gents", "male ")
KID_WORDS = ("kid", "girl", "baby", "child", "boys", "boy ", "infant", "toddler")

SEARCH_JS = """async ([q, page]) => {
  const r = await fetch('/api/v1/products/search', {method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({query: q, type: 'text_search', page: page, offset: (page - 1) * 20, limit: 20,
                          // Meesho's own "Rating: 4.0 And Above" filter (captured from the site)
                          selected_filters: ['eyJmaWVsZCI6ImxhYmVscy4xNiIsIm9wIjoiaW4iLCJ2YWx1ZSI6IjQ1OCJ9'],
                          selectedFilterIds: [458], cursor: null, isDevicePhone: false})});
  if (!r.ok) return {error: r.status};
  return await r.json();
}"""

log = logging.getLogger("catalog")


def norm(name: str) -> str:
    return re.sub(r"[^a-z]+", " ", (name or "").lower()).strip()


def score(rating: float, count: int) -> float:
    return rating * math.log10(1 + count)


def discover(more: bool = False, queries_by_group: dict | None = None):
    """Search Meesho and write ranked candidates. `more` / explicit queries merge into the existing file."""
    from pipeline.scraper import HOME, MeeshoScraper
    merge = more or queries_by_group is not None
    queries_by_group = queries_by_group or (EXTRA_QUERIES if more else QUERIES)
    s = MeeshoScraper()
    s._connect()
    page = s._browser.contexts[0].new_page()
    page.goto(HOME, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    page.goto("https://www.meesho.com/search?q=kurti", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    found = {}
    if merge and CANDIDATES.exists():  # merge into what earlier searches found
        for items in json.loads(CANDIDATES.read_text(encoding="utf-8")).values():
            for item in items:
                found[item["product_id"]] = item
    try:
        for group, queries in queries_by_group.items():
            lo, hi = PRICE_RANGE[group]
            for q in queries:
                for pg in range(1, PAGES + 1):
                    res = page.evaluate(SEARCH_JS, [q, pg])
                    if "error" in res:
                        print(f"  ! {q} p{pg}: HTTP {res['error']}", flush=True)
                        if res["error"] in (403, 429):
                            time.sleep(60)
                        break
                    cats = res.get("catalogs") or []
                    for c in cats:
                        pid, rs = c.get("product_id"), c.get("catalog_reviews_summary") or {}
                        name = c.get("hero_product_name") or c.get("name") or ""
                        low = f" {name.lower()} "
                        rating, count = float(rs.get("average_rating") or 0), int(rs.get("rating_count") or 0)
                        price = c.get("min_product_price") or 0
                        if (not pid or not c.get("valid", True) or rating < MIN_RATING or count < MIN_COUNT
                                or not lo <= price <= hi or any(w in low for w in MEN_WORDS)):
                            continue
                        g = group
                        if group == "women" and any(w in low for w in KID_WORDS):
                            g = "kids"
                        if group == "kids" and not any(w in low for w in KID_WORDS) and "kids" not in q:
                            continue  # adult item that showed up in a kids search
                        prev = found.get(pid)
                        item = {"group": g, "query": q, "product_id": pid,
                                "url": f"https://www.meesho.com/{c.get('slug')}/p/{pid}", "name": name,
                                "category": c.get("sub_sub_category_name"), "price": price, "rating": rating,
                                "rating_count": count, "score": round(score(rating, count), 3),
                                "image": c.get("image")}
                        if not prev or item["score"] > prev["score"]:
                            found[pid] = item
                    if len(cats) < 20:
                        break
                    time.sleep(random.uniform(1.2, 2.2))
                n = sum(1 for v in found.values() if v["group"] == group)
                print(f"{group:<10} {q:<36} -> {n} candidates", flush=True)
    finally:
        page.close()
        s.close()
    out = {}
    for item in found.values():
        out.setdefault(item["group"], []).append(item)
    for g in out:
        out[g].sort(key=lambda x: -x["score"])
    CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATES.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print({g: len(v) for g, v in out.items()}, "->", CANDIDATES)


def regroup(item: dict) -> str | None:
    """Final store group from Meesho's category (search queries leak across groups)."""
    cat = (item.get("category") or "").strip()
    low = f" {(item.get('name') or '').lower()} "
    kid = any(w in low for w in KID_WORDS)
    if cat in GROUP_BY_CATEGORY["jewellery"]:
        return "jewellery"
    if cat in GROUP_BY_CATEGORY["nightwear"] or (cat == "Top & Bottom Sets" and any(w in low for w in NIGHT_WORDS)):
        return "nightwear" if not kid else None
    if cat in GROUP_BY_CATEGORY["kids"] and cat not in GROUP_BY_CATEGORY["women"]:
        return "kids"
    if cat in GROUP_BY_CATEGORY["women"]:
        # "Ethnic Gowns" exists for both: decide by the title
        return "kids" if cat == "Ethnic Gowns" and kid else "women"
    return None  # western dresses, track pants, fabric etc.


def select(n: int = 300, out=SELECTED) -> list[dict]:
    from pipeline.sheet import Sheet
    existing = {str(r["meesho_id"]) for r in Sheet().all()}
    data = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    pool = {}
    for items in data.values():
        for item in items:
            g = regroup(item)
            if g:
                pool.setdefault(g, []).append({**item, "group": g})
    picks = []
    for group in QUERIES:
        seen_names, seen_images, seen_reviews, chosen = set(), set(), set(), []
        for item in sorted(pool.get(group, []), key=lambda x: -x["score"]):
            key = (norm(item["name"]), item["price"])
            img = (item.get("image") or "").split("?")[0]
            # sellers listing the same catalog share one review pool -> identical rating + count
            rev = (item["rating"], item["rating_count"]) if item["rating_count"] >= 50 else None
            if (str(item["product_id"]) in existing or key in seen_names or (img and img in seen_images)
                    or (rev and rev in seen_reviews)):
                continue
            seen_names.add(key)
            if rev:
                seen_reviews.add(rev)
            if img:
                seen_images.add(img)
            chosen.append(item)
            if len(chosen) == n:
                break
        print(f"{group:<10} picked {len(chosen)}/{n} (from {len(pool.get(group, []))} candidates; "
              f"lowest pick {chosen[-1]['rating']}★ x {chosen[-1]['rating_count']})" if chosen else
              f"{group:<10} picked 0")
        picks += chosen
    # interleave categories so every category grows evenly while the run is in progress
    by_group = {g: [p for p in picks if p["group"] == g] for g in QUERIES}
    order = []
    while any(by_group.values()):
        for g in QUERIES:
            if by_group[g]:
                order.append(by_group[g].pop(0))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(order, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(order)} picks -> {out}")
    return order


def run(limit: int | None = None, picks_file=SELECTED) -> dict:
    from pipeline.workflow import Pipeline
    picks = json.loads(picks_file.read_text(encoding="utf-8"))
    p = Pipeline()
    done = {str(r["meesho_id"]): r["status"] for r in p.sheet.all()}
    ok = failed = skipped = 0
    blocked_streak = 0
    try:
        for i, item in enumerate(picks, 1):
            mid = str(item["product_id"])
            if done.get(mid) in ("PUSHED", "REJECTED", "UNAVAILABLE"):
                skipped += 1
                continue
            if limit and ok >= limit:
                break
            t0 = time.time()
            try:
                if done.get(mid) not in ("READY", "APPROVED") or not (p.data_dir(mid) / "copy.json").exists():
                    summary = p.process(item["url"], "catalog")
                    if summary.get("status") == "UNAVAILABLE":
                        skipped += 1
                        continue
                p.sheet.upsert(mid, collections=COLLECTION[item["group"]])
                p.push(mid)
                p.set_live(mid, True)
                ok += 1
                blocked_streak = 0
                print(f"[{i}/{len(picks)}] OK   {mid} {item['group']:<9} {item['rating']}★x{item['rating_count']:<6} "
                      f"{item['name'][:45]} ({time.time() - t0:.0f}s)", flush=True)
            except Exception as e:
                failed += 1
                log.exception("[%d/%d] FAIL %s", i, len(picks), mid)
                print(f"[{i}/{len(picks)}] FAIL {mid} {item['group']:<9} {str(e)[:120]}", flush=True)
                if "block" in str(e).lower() or "403" in str(e):
                    blocked_streak += 1
                    wait = min(1800, 300 * blocked_streak)
                    print(f"Meesho is blocking; pausing {wait // 60} min", flush=True)
                    time.sleep(wait)
            time.sleep(random.uniform(4, 9))
    finally:
        p.close()
    print(f"DONE ok={ok} failed={failed} skipped={skipped}", flush=True)
    return {"ok": ok, "failed": failed, "skipped": skipped}


# --- daily growth ---------------------------------------------------------------------------------
# Base items x modifiers = hundreds of distinct searches per category; each day searches the next slice,
# so new products keep turning up and the candidate file grows (already-listed products are skipped).
MODIFIERS = {
    "women": ["cotton", "silk", "rayon", "georgette", "chiffon", "printed", "embroidered", "festive", "party wear",
              "wedding", "daily wear", "office wear", "navratri", "diwali", "red", "yellow", "green", "pink",
              "black", "maroon", "blue", "white", "plus size", "new design"],
    "kids": ["festive", "party wear", "cotton", "silk", "navratri", "diwali", "wedding", "1 year", "3 year",
             "5 year", "8 year", "12 year", "red", "pink", "yellow", "new design"],
    "jewellery": ["gold plated", "oxidised", "kundan", "pearl", "silver", "party wear", "bridal", "daily wear",
                  "traditional", "stylish", "combo", "antique", "rose gold", "meenakari", "new design"],
    "nightwear": ["cotton", "printed", "satin", "rayon", "full sleeve", "half sleeve", "maternity", "plus size",
                  "summer", "winter", "hosiery", "new design"],
}
QUERIES_PER_GROUP_PER_DAY = 25


def all_queries(group: str) -> list[str]:
    bases = list(dict.fromkeys(QUERIES[group] + EXTRA_QUERIES[group]))
    combos = [f"{m} {b}" for b in bases for m in MODIFIERS[group] if m not in b]
    random.Random(group).shuffle(combos)          # fixed order, so the daily slices don't repeat
    return bases + combos


def todays_queries(day: int) -> dict:
    out = {}
    for g in QUERIES:
        qs, k = all_queries(g), QUERIES_PER_GROUP_PER_DAY
        start = (day * k) % len(qs)
        out[g] = (qs + qs)[start:start + k]
    return out


def daily(n: int = 100):
    """discover today's slice -> select top n new winners per category -> push them. Re-running on the
    same day skips straight to the push and continues where it stopped."""
    from datetime import date
    lock = DAILY_DIR / "running.lock"
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    if lock.exists() and time.time() - lock.stat().st_mtime < 20 * 3600:
        print(f"Another daily run is still going (lock {lock}); delete the lock if it isn't.", flush=True)
        return
    lock.write_text(str(os.getpid()))
    try:
        today = date.today()
        picks_file = DAILY_DIR / f"{today:%Y%m%d}.json"
        if not picks_file.exists():
            print(f"== {today}: searching Meesho ({QUERIES_PER_GROUP_PER_DAY} new queries per category)", flush=True)
            discover(queries_by_group=todays_queries(today.toordinal()))
            print(f"== picking the top {n} new winners per category", flush=True)
            select(n, out=picks_file)
        else:
            print(f"== {today}: resuming {picks_file.name}", flush=True)
        res = run(picks_file=picks_file)
        picks = json.loads(picks_file.read_text(encoding="utf-8"))
        print("== picked today: " + ", ".join(f"{g} {sum(p['group'] == g for p in picks)}" for g in QUERIES), flush=True)
        print(f"== DAILY DONE {today}: {res}", flush=True)
    finally:
        lock.unlink(missing_ok=True)


def status():
    from pipeline.sheet import Sheet
    picks = json.loads(SELECTED.read_text(encoding="utf-8"))
    st = {str(r["meesho_id"]): r["status"] for r in Sheet().all()}
    for g in QUERIES:
        mine = [p for p in picks if p["group"] == g]
        print(f"{g:<10} {sum(st.get(str(p['product_id'])) == 'PUSHED' for p in mine)}/{len(mine)} live")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    setup_logging("catalog")
    cmd, args = sys.argv[1], sys.argv[2:]
    {"discover": lambda: discover(bool(args and args[0] == "more")), "status": status,
     "select": lambda: select(int(args[0]) if args else 300),
     "run": lambda: run(int(args[0]) if args else None),
     "daily": lambda: daily(int(args[0]) if args else 100)}[cmd]()
