import math

from .config import CFG


def round_up_ending(value: float, ending: int | None) -> int:
    """Round up to the next whole number ending in `ending` (9 -> 579, 99 -> 599)."""
    x = math.ceil(value)
    if not ending:
        return x
    mod = 10 ** len(str(ending))
    candidate = x - x % mod + ending
    return candidate if candidate >= x else candidate + mod


def cost_of(meesho_price: float, shipping: float = 0) -> float:
    return meesho_price + (shipping or 0) + CFG["pricing"].get("extra_cost", 0)


def sell_price(cost: float) -> int:
    p = CFG["pricing"]
    raw = cost * (1 + p["margin_percent"] / 100) + p["fixed_add"]
    return round_up_ending(raw, p.get("round_ending"))


def compare_at_price(price: float, mrp: float | None) -> int | None:
    mode = CFG["pricing"].get("compare_at", "mrp")
    if mode in (None, "none"):
        return None
    if mode == "mrp":
        return int(mrp) if mrp and mrp > price else None
    return round_up_ending(price * float(mode), CFG["pricing"].get("round_ending"))


def price_variants(product: dict, override: float | None = None) -> list[dict]:
    """Returns the scraped variants with cost / price / compare_at filled in."""
    out = []
    for v in product["variants"]:
        cost = cost_of(v["price"], product.get("shipping_charges"))
        price = int(override) if override else sell_price(cost)
        out.append({**v, "cost": cost, "my_price": price,
                    "compare_at": compare_at_price(price, product.get("mrp"))})
    return out


def price_label(values: list[int]) -> str:
    lo, hi = min(values), max(values)
    return str(lo) if lo == hi else f"{lo}-{hi}"
