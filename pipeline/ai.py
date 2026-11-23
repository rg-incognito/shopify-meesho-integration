import io
import json
import logging
import time
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

from .config import CFG, env

log = logging.getLogger(__name__)

COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description_html": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "seo_title": {"type": "string"},
        "seo_description": {"type": "string"},
    },
    "required": ["title", "description_html", "tags", "seo_title", "seo_description"],
}

COPY_PROMPT = """You write product listings for {store}, an Indian online fashion store on Shopify.
Rewrite this supplier listing into original, appealing copy for our store.

Rules:
- title: max 70 characters, natural and descriptive (product type, fabric, key design detail). No ALL CAPS, no supplier or marketplace names.
- description_html: 2 short paragraphs (<p>) then a <ul> of 4-6 key features. Use only facts present in the data below; never invent sizes, measurements, fabric or care instructions that are not given.
- tags: 5-10 lowercase tags (category, fabric, occasion, colour, style).
- seo_title: max 60 characters. seo_description: max 155 characters.
- Never mention Meesho, the supplier, ratings, or prices.

Supplier data:
{data}
"""


def template_copy(product: dict) -> dict:
    """Listing copy without AI: cleaned-up Meesho title plus the supplier's facts."""
    import html
    import re

    attrs = product.get("attributes", {})
    raw = re.sub(r"\s+", " ", product["title"]).strip()
    words, seen = [], set()
    for w in raw.split():
        k = w.lower().strip(",.|-")
        if k and k not in seen:
            seen.add(k)
            words.append(w if not w.isupper() or len(w) <= 3 else w.title())
    title = " ".join(words)[:70].rstrip(" ,|-")
    if len(title) < 12:  # e.g. "Lahanga" -> add facts
        title = " ".join(x for x in (attrs.get("Color", ""), attrs.get("Fabric", ""), product["category"]) if x)
    desc_lines = [l.strip() for l in (product.get("description") or "").splitlines()
                  if l.strip() and not l.lower().startswith(("name:", "country of origin"))]
    features = "".join(f"<li>{html.escape(l)}</li>" for l in desc_lines[:6])
    intro = f"{html.escape(title)} from {html.escape(CFG['store']['name'])}, chosen for festive and everyday wear."
    tags = sorted({v.lower() for k, v in attrs.items()
                   if k in ("Color", "Fabric", "Pattern", "Occasion", "Ocassion", "Type") and len(v) < 25}
                  | {product["category"].lower()})
    return {"title": title, "description_html": f"<p>{intro}</p><ul>{features}</ul>", "tags": tags,
            "seo_title": title[:60], "seo_description": intro[:155], "_model": "template"}


def _details(product: dict) -> str:
    keep = ("Color", "Fabric", "Pattern", "Print or Pattern Type", "Surface Styling", "Border", "Type",
            "Occasion", "Ocassion", "Sleeve Length", "Neck/ Collar", "Fit/ Shape", "Length")
    return ", ".join(f"{k}: {v}" for k, v in product["attributes"].items() if k in keep) or product["category"]


class GeminiAI:
    def __init__(self):
        self.cfg = CFG["gemini"]
        self.client = genai.Client(api_key=env("GEMINI_API_KEY"))

    _exhausted: dict[str, float] = {}  # model -> time it may be retried (daily free quotas)

    def _call(self, retries: int = 3, **kwargs):
        for attempt in range(retries):
            try:
                return self.client.models.generate_content(**kwargs)
            except Exception as e:
                # quota/billing errors won't fix themselves within seconds
                if (attempt == retries - 1 or "limit: 0" in str(e) or "PERMISSION_DENIED" in str(e)
                        or "PerDay" in str(e)):
                    raise
                log.warning("Gemini call failed (%s), retrying", e)
                time.sleep(5 * (attempt + 1))

    def _text_models(self) -> list[str]:
        models = self.cfg.get("text_models") or [self.cfg["text_model"]]
        now = time.time()
        return [m for m in models if self._exhausted.get(m, 0) <= now]

    def write_copy(self, product: dict) -> dict:
        """AI listing copy; tries each configured text model in turn (each has its own free quota)
        and falls back to a template built from the Meesho data if all are unavailable."""
        data = {k: product[k] for k in ("title", "description", "category", "attributes")}
        data["sizes"] = [v["name"] for v in product["variants"]]
        for model in self._text_models():
            try:
                resp = self._call(
                    model=model,
                    contents=COPY_PROMPT.format(store=CFG["store"]["name"],
                                                data=json.dumps(data, ensure_ascii=False, indent=1)),
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_json_schema=COPY_SCHEMA,
                        temperature=0.7,
                    ),
                )
                copy = json.loads(resp.text)
                copy["_model"] = model
                return copy
            except Exception as e:
                if "RESOURCE_EXHAUSTED" in str(e) or "UNAVAILABLE" in str(e) or "NOT_FOUND" in str(e):
                    log.warning("Text model %s unavailable (%s); trying the next one", model, str(e)[:80])
                    self._exhausted[model] = time.time() + (3 * 3600 if "PerDay" in str(e) else 120)
                    continue
                raise
        log.warning("All Gemini text models unavailable; using template copy")
        return template_copy(product)

    def generate_images(self, product: dict, reference_paths: list[Path], out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("*.jpg"):
            old.unlink()
        refs = [Image.open(p).convert("RGB") for p in reference_paths[: self.cfg["reference_images"]]]
        title, details = product["title"], _details(product)
        made = []
        for shot in self.cfg["shots"]:
            try:
                resp = self._call(
                    model=self.cfg["image_model"],
                    contents=[shot["prompt"].format(title=title, details=details), *refs],
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                        image_config=types.ImageConfig(
                            aspect_ratio=shot.get("aspect_ratio") or self.cfg["aspect_ratio"]),
                    ),
                )
                image_bytes = next(
                    (p.inline_data.data for c in resp.candidates or [] for p in (c.content.parts or [])
                     if p.inline_data and p.inline_data.data),
                    None,
                )
                if not image_bytes:
                    log.warning("No image returned for shot %s", shot["name"])
                    continue
                path = out_dir / f"{len(made) + 1:02d}-{shot['name']}.jpg"
                Image.open(io.BytesIO(image_bytes)).convert("RGB").save(path, "JPEG", quality=92)
                made.append(path)
            except Exception as e:
                if "limit: 0" in str(e) or "PERMISSION_DENIED" in str(e):
                    raise  # every other shot would fail the same way
                log.error("Image shot %s failed: %s", shot["name"], e)
        if not made:
            raise RuntimeError("Gemini did not return any images")
        return made
