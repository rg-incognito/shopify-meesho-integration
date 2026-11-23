"""Find well-rated Meesho products for a theme and write a ranked shortlist.

  python discover.py            -> data/discovery/navratri.json (candidates per group)

Uses Meesho's own search API from inside the (already warmed-up) browser session.
"""
import json
import math
import sys
import time

from pipeline.config import DATA_DIR, setup_logging
from pipeline.scraper import HOME, MeeshoScraper

# group -> search queries. Groups map to Shopify collections in bulk_add.py
QUERIES = {
    "women": ["navratri chaniya choli women", "garba chaniya choli", "navratri lehenga choli women",
              "bandhani lehenga choli", "mirror work chaniya choli", "navratri special lehenga",
              "gamthi work chaniya choli", "kutchi work lehenga choli", "chaniya choli for women",
              "navratri ghagra choli", "cotton chaniya choli", "rajasthani lehenga choli", "ajrakh lehenga choli",
              "readymade chaniya choli women", "garba dress for women", "kedia dress women navratri",
              "navratri kurti women", "lehenga choli for women"],
    "kids": ["kids chaniya choli navratri", "girls navratri lehenga choli", "kids lehenga choli garba",
             "baby girl chaniya choli", "boys kediyu navratri", "kids navratri dress"],
    "jewellery": ["oxidised jewellery set navratri", "navratri oxidised earrings", "garba jewellery set",
                  "oxidised necklace set women", "navratri kamarbandh", "oxidised choker set"],
    "dupatta": ["navratri dupatta", "bandhani dupatta", "mirror work dupatta", "phulkari dupatta"],
}
PAGES = 3
MIN_RATING, MIN_COUNT = 3.9, 40
MIN_COUNT_BY_GROUP = {"women": 15}
PRICE_RANGE = {"women": (250, 2000), "kids": (180, 1200), "jewellery": (80, 700), "dupatta": (120, 700)}
MEN_WORDS = ("men's", " men ", "mens ", "for men", "boys kurta", "gents")

SEARCH_JS = """async ([q, page]) => {
  const r = await fetch('/api/v1/products/search', {method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({query: q, type: 'text_search', page: page, offset: (page - 1) * 20, limit: 20,
                          cursor: null, isDevicePhone: false})});
  if (!r.ok) return {error: r.status};
  return await r.json();
}"""


def score(c: dict) -> float:
    rs = c.get("catalog_reviews_summary") or {}
    return float(rs.get("average_rating") or 0) * math.log10(1 + int(rs.get("rating_count") or 0))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    setup_logging("discover")
    s = MeeshoScraper()
    s._connect()
    page = s._browser.contexts[0].new_page()
    page.goto(HOME, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    page.goto("https://www.meesho.com/search?q=lehenga", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    out, seen = {}, set()
    PAGES_BY_GROUP = {"women": 4}
    for group, queries in QUERIES.items():
        lo, hi = PRICE_RANGE[group]
        found = {}
        for q in queries:
            for pg in range(1, PAGES_BY_GROUP.get(group, PAGES) + 1):
                res = page.evaluate(SEARCH_JS, [q, pg])
                if "error" in res:
                    print(f"  ! {q} p{pg}: HTTP {res['error']}")
                    break
                for c in res.get("catalogs") or []:
                    pid, rs = c.get("product_id"), c.get("catalog_reviews_summary") or {}
                    name = (c.get("hero_product_name") or c.get("name") or "").lower()
                    price = c.get("min_product_price") or 0
                    if (not pid or pid in seen or not c.get("valid", True)
                            or float(rs.get("average_rating") or 0) < MIN_RATING
                            or int(rs.get("rating_count") or 0) < MIN_COUNT_BY_GROUP.get(group, MIN_COUNT)
                            or not lo <= price <= hi
                            or any(w in f" {name} " for w in MEN_WORDS)):
                        continue
                    seen.add(pid)
                    g = "kids" if group == "women" and any(w in name for w in ("kid", "girl", "baby", "child")) else group
                    found[pid] = {
                        "group": g, "query": q, "product_id": pid,
                        "url": f"https://www.meesho.com/{c.get('slug')}/p/{pid}",
                        "name": c.get("hero_product_name") or c.get("name"),
                        "category": c.get("sub_sub_category_name"), "price": price,
                        "rating": rs.get("average_rating"), "rating_count": rs.get("rating_count"),
                        "score": round(score(c), 3), "image": c.get("image"),
                    }
                time.sleep(1.5)
            print(f"{group:<10} {q:<38} -> {len(found)} candidates so far")
        for item in found.values():
            out.setdefault(item["group"], []).append(item)
    for g in out:
        out[g].sort(key=lambda x: -x["score"])
    page.close()
    s.close()

    path = DATA_DIR / "discovery" / "navratri.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for g, items in out.items():
        print(f"\n== {g}: {len(items)} candidates; top 5:")
        for it in items[:5]:
            print(f"   {it['rating']}★ ({it['rating_count']})  ₹{it['price']:<5} {it['category']:<18} {it['name'][:60]}")
    print("\nSaved", path)


if __name__ == "__main__":
    main()
