"""Free, CPU-only "studio" photos: cut the product out of the Meesho photo (rembg) and place it
on a clean branded background with a soft shadow. The product itself is never altered.
"""
import logging
from pathlib import Path

from PIL import Image, ImageFilter

from .config import CFG

log = logging.getLogger(__name__)

_session = None


def _rembg_session():
    global _session
    if _session is None:
        from rembg import new_session
        _session = new_session(CFG.get("studio", {}).get("model", "isnet-general-use"))
    return _session


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def studio_photo(src: Path, dst: Path) -> bool:
    """Writes a studio version of `src` to `dst`. Returns False when the photo isn't a good
    candidate (size charts, collages, text graphics), in which case nothing is written."""
    from rembg import remove

    cfg = CFG.get("studio", {})
    w, h = (int(x) for x in cfg.get("size", [1500, 2000]))
    with Image.open(src) as im:
        im = im.convert("RGB")
        cut = remove(im, session=_rembg_session(), post_process_mask=True)
    alpha = cut.getchannel("A")
    box = alpha.point(lambda a: 255 if a > 40 else 0).getbbox()
    if not box:
        return False
    coverage = sum(1 for a in alpha.getdata() if a > 128) / (alpha.width * alpha.height)
    # size charts / text graphics leave only thin lines; a near-full mask means nothing was separated
    if coverage < cfg.get("min_coverage", 0.10) or coverage > cfg.get("max_coverage", 0.97):
        log.info("Skipping %s for studio (mask coverage %.2f)", src.name, coverage)
        return False

    product = cut.crop(box)
    pad = cfg.get("padding", 0.08)
    scale = min(w * (1 - 2 * pad) / product.width, h * (1 - 2 * pad) / product.height)
    product = product.resize((max(1, round(product.width * scale)), max(1, round(product.height * scale))),
                             Image.LANCZOS)

    canvas = Image.new("RGB", (w, h), _hex(cfg.get("background", "#F4EDE1")))
    x = (w - product.width) // 2
    y = (h - product.height) // 2 + round(h * 0.01)
    # soft contact shadow under the product
    shadow = Image.new("L", (w, h), 0)
    sh = product.getchannel("A").point(lambda a: int(a * cfg.get("shadow_strength", 0.35)))
    shadow.paste(sh, (x + round(w * 0.012), y + round(h * 0.015)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=w * 0.02))
    canvas.paste(Image.new("RGB", (w, h), (90, 70, 40)), (0, 0), shadow)
    canvas.paste(product, (x, y), product)
    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dst, "JPEG", quality=92)
    return True


def make_studio_images(originals: list[Path], out_dir: Path, limit: int | None = None) -> list[Path]:
    """Studio versions of the originals, in order; photos that can't be cut out cleanly
    (size charts etc.) are kept as-is so the listing still has them."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    made = []
    for i, src in enumerate(originals[:limit] if limit else originals, 1):
        dst = out_dir / f"{i:02d}-studio.jpg"
        try:
            if studio_photo(src, dst):
                made.append(dst)
                continue
        except Exception as e:
            log.warning("Studio failed for %s: %s", src.name, e)
        keep = out_dir / f"{i:02d}-orig.jpg"
        with Image.open(src) as im:
            im.convert("RGB").save(keep, "JPEG", quality=92)
        made.append(keep)
    return made
