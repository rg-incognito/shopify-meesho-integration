"""Single-image post prompts from the prompts/ library (women / men / kid, one .txt per image style).

For each product: pick the folder from the product's audience, take the style used least recently
(never the one this product had last time), and let Gemini (API, with the product photo) fill the
[PLACEHOLDERS] with the product's real details. The filled prompt goes to the Gemini app.
See prompts/README.md for why these styles.
"""
import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR, ROOT

log = logging.getLogger(__name__)

PROMPTS_DIR = ROOT / "prompts"
USAGE_FILE = DATA_DIR / "prompt_usage.json"
PLACEHOLDER = re.compile(r"\[([A-Z][A-Z0-9 /&'-]*?)(?:,\s*e\.g\.,?\s*([^\]]*))?\]")
MEN = re.compile(r"\bmen\b|\bmens\b|\bmen's\b|\bman\b", re.I)

# how faces are shown (posts so far always hide them; "visible" = natural face looking away)
FACE_RULES = {
    "hidden": "The face must not be visible: hidden by a hand, hair, a phone, turned away or out of frame.",
    "visible": "A natural, relaxed face is allowed, looking away from the camera or in soft profile.",
}

FILL_PROMPT = """You prepare an image-generation prompt for the Gemini app. The attached photo is the product
(an Indian ethnic-wear garment); the image will be generated from this photo plus the prompt.

Product data from the seller (may be wrong about colours: trust the PHOTO for colours and details):
{facts}

Template:
<<<
{template}
>>>

Rewrite the template with every [PLACEHOLDER] (written as [NAME] or [NAME, e.g., example]) replaced by a
short, specific value that fits THIS product as seen in the photo: real colours, fabric, embroidery/print,
pieces included (only mention a dupatta, jacket, stole etc. if the product has one). The examples are only
examples: never copy them if they don't match the product. For [GIRL/BOY] and [AGE] use: {kid}.
{face_rule} If a placeholder line doesn't apply (e.g. a layer the product doesn't have), drop that line.
Replace any reference to a specific file name (like image_5.png) with "the uploaded product photo".
Keep everything else exactly as written. Output only the final prompt text, no brackets left, no comments."""


def folder_for(mid: str, prow: dict) -> str | None:
    """women / men / kid, or None for products the library doesn't cover (jewellery etc.)."""
    from .posts import _load, audience, garment
    aud = audience(mid, prow)
    if aud == "kids":
        return "kid"
    if aud != "women":
        return None
    product = _load(mid, "product")
    text = " ".join([garment(mid), str(product.get("title", "")), str(product.get("category", ""))])
    if re.search(r"\bwomen|\bgirls?\b|\bladies\b", text, re.I):
        return "women"
    return "men" if MEN.search(text) else "women"


def templates(folder: str) -> list[Path]:
    return sorted((PROMPTS_DIR / folder).glob("*.txt"))


def _usage() -> dict:
    return json.loads(USAGE_FILE.read_text(encoding="utf-8")) if USAGE_FILE.exists() else {"last": {}, "by_mid": {}}


def pick(folder: str, mid: str, rng: random.Random | None = None) -> Path:
    """The style used least recently in this folder (ties broken at random), not this product's last one."""
    rng = rng or random.Random()
    files = templates(folder)
    if not files:
        raise RuntimeError(f"no prompts in prompts/{folder}/")
    u = _usage()
    before = u["by_mid"].get(mid)
    choices = [f for f in files if f.name != before] or files
    rng.shuffle(choices)
    return min(choices, key=lambda f: u["last"].get(f"{folder}/{f.name}", ""))


def mark_used(folder: str, template: Path, mid: str) -> None:
    u = _usage()
    u["last"][f"{folder}/{template.name}"] = datetime.now().isoformat(timespec="seconds")
    u["by_mid"][mid] = template.name
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(u, indent=1), encoding="utf-8")


def _kid_words(product: dict) -> str:
    title = f"{product.get('title', '')} {product.get('category', '')}".lower()
    sex = "girl" if "girl" in title or "lehenga" in title or "frock" in title else \
        "boy" if "boy" in title or "sherwani" in title else "girl or boy as suits the outfit"
    ages = [int(n) for v in product.get("variants", []) if v.get("in_stock")
            for n in re.findall(r"(\d+)\s*-?\s*\d*\s*(?:years|yrs)", v.get("name", ""), re.I)]
    age = f"{max(3, min(10, sorted(ages)[len(ages) // 2]))}-year-old" if ages else "6-year-old"
    return f"a {age} {sex}"


def _fallback(template: str, mid: str, kid: str, face_rule: str) -> str:
    """Without the API: the garment line gets the product name, other placeholders a neutral phrase."""
    from .posts import garment
    def sub(m):
        name = m.group(1)
        if name.startswith("GARMENT"):
            return garment(mid) or "the outfit in the uploaded product photo"
        if name in ("GIRL/BOY", "AGE"):
            return kid
        if name.startswith("FACE"):
            return face_rule
        return "chosen to suit the outfit in the uploaded product photo"
    return PLACEHOLDER.sub(sub, template)


def fill(template: Path, mid: str, photo: Path, faces: str = "hidden") -> str:
    from google.genai import types
    from .ai import GeminiAI
    from .posts import _load, garment
    text = template.read_text(encoding="utf-8")
    product = _load(mid, "product")
    kid = _kid_words(product)
    face_rule = FACE_RULES.get(faces, FACE_RULES["hidden"])
    facts = f"Name: {garment(mid) or product.get('title', '')}\nCategory: {product.get('category', '')}\n" \
            "Details: " + ", ".join(f"{k}: {v}" for k, v in (product.get("attributes") or {}).items()
                                    if k not in ("Country of Origin", "Net Quantity (N)"))
    g = GeminiAI()
    for model in g._text_models():
        try:
            resp = g._call(retries=1, model=model, contents=[   # busy model: go straight to the next
                types.Part.from_bytes(data=photo.read_bytes(), mime_type="image/jpeg"),
                FILL_PROMPT.format(facts=facts, template=text, kid=kid, face_rule=face_rule)],
                config=types.GenerateContentConfig(temperature=0.4))
            out = (resp.text or "").strip().strip("<>").strip()
            if len(out) > len(text) * 0.5 and not PLACEHOLDER.search(out):
                return out
            log.warning("filled prompt from %s looked wrong; trying the next model", model)
        except Exception as e:
            log.warning("filling prompt via %s failed: %s", model, str(e)[:150])
            if "RESOURCE_EXHAUSTED" in str(e):
                g._exhausted[model] = time.time() + (3 * 3600 if "PerDay" in str(e) else 120)
    return _fallback(text, mid, kid, face_rule)


def make(mid: str, prow: dict, photo: Path, faces: str = "hidden") -> dict | None:
    """{folder, style, prompt} for one single-image post, or None if no folder fits the product."""
    folder = folder_for(mid, prow)
    if not folder:
        return None
    t = pick(folder, mid)
    return {"folder": folder, "style": t.stem, "prompt": fill(t, mid, photo, faces), "template": t}
