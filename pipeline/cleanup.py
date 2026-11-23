"""Listing photo clean-up (no AI, never alters the product itself):
  1. paint out Meesho's supplier code ("S-775771012", white text in the bottom-left corner)
  2. pad square/landscape photos to 4:5 portrait with a blurred fill, so product cards don't crop them
"""
import logging
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

log = logging.getLogger(__name__)

PORTRAIT = 0.8  # width / height of the store's product cards


def remove_supplier_code(im: Image.Image) -> tuple[Image.Image, bool]:
    """Finds a row of small, bright, text-like marks in the bottom-left corner and inpaints them."""
    rgb = np.asarray(im.convert("RGB"))
    h, w = rgb.shape[:2]
    y0, x1 = int(h * 0.80), int(w * 0.55)
    roi = rgb[y0:, :x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
    bright = ((hsv[..., 2] > 215) & (hsv[..., 1] < 40)).astype(np.uint8)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(bright, connectivity=8)
    chars = []
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if 0.012 * h <= ch <= 0.07 * h and cw <= 0.06 * w and area >= 12 and x + cw < x1:
            chars.append((i, cents[i][1], ch))
    if len(chars) < 5:
        return im, False
    # the code is one line of similar-height characters: take the biggest row group
    # group into rows; the code is the LOWEST row of >=5 similar-height marks starting at the left edge
    chars.sort(key=lambda c: c[1])
    rows, group = [], []
    for c in chars:
        if group and abs(c[1] - group[0][1]) > 0.02 * h:
            rows.append(group)
            group = []
        group.append(c)
    rows.append(group)
    best = []
    for row in reversed(rows):
        hs = np.array([c[2] for c in row], float)
        left = min(stats[i][0] for i, _, _ in row)
        if len(row) >= 5 and left < 0.08 * w and hs.std() / hs.mean() < 0.35:
            best = row
            break
    if not best:
        return im, False
    # paint out the whole strip the code occupies (its grey outline isn't "bright")
    xs = [stats[i][0] for i, _, _ in best] + [stats[i][0] + stats[i][2] for i, _, _ in best]
    ys = [stats[i][1] for i, _, _ in best] + [stats[i][1] + stats[i][3] for i, _, _ in best]
    pad = int(h * 0.008)
    mask = np.zeros((h, w), np.uint8)
    mask[max(0, y0 + min(ys) - pad):min(h, y0 + max(ys) + pad), 0 if min(xs) < 0.08 * w else max(0, min(xs) - pad):min(w, max(xs) + pad)] = 255
    out = cv2.inpaint(rgb, mask, max(3, int(h * 0.01)), cv2.INPAINT_TELEA)
    return Image.fromarray(out), True


def pad_to_portrait(im: Image.Image, ratio: float = PORTRAIT) -> tuple[Image.Image, bool]:
    """Photo -> 4:5 canvas (wider or taller as needed), centred on its own background colour
    or a soft blurred copy of itself."""
    w, h = im.size
    if ratio * 0.88 <= w / h <= ratio * 1.06:  # close enough to 4:5
        return im, False
    if w / h < ratio:  # very tall photo: widen it instead (cards would cut off head and hem)
        nw = int(round(h * ratio))
        a = np.asarray(im.convert("RGB")).astype(np.int16)
        b = max(2, w // 60)
        edge = np.concatenate([a[:, :b].reshape(-1, 3), a[:, -b:].reshape(-1, 3)])
        med = np.median(edge, axis=0)
        if (np.abs(edge - med).max(axis=1) < 20).mean() >= 0.6:
            bg = Image.new("RGB", (nw, h), tuple(int(v) for v in med))
        else:
            bg = im.resize((nw, int(h * nw / w)), Image.LANCZOS)
            top = (bg.height - h) // 2
            bg = bg.crop((0, top, nw, top + h)).filter(ImageFilter.GaussianBlur(radius=max(30, nw // 15)))
            bg = ImageEnhance.Brightness(bg).enhance(0.9)
        bg.paste(im, ((nw - w) // 2, 0))
        return bg, True
    nh = int(round(w / ratio))
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    b = max(2, h // 60)
    edge = np.concatenate([a[:b].reshape(-1, 3), a[-b:].reshape(-1, 3),
                           a[:, :b].reshape(-1, 3), a[:, -b:].reshape(-1, 3)])
    med = np.median(edge, axis=0)
    if (np.abs(edge - med).max(axis=1) < 20).mean() >= 0.6:  # plain studio/white background: extend it
        bg = Image.new("RGB", (w, nh), tuple(int(v) for v in med))
    else:  # busy scene: soft blurred copy of the photo behind it
        bg = im.resize((int(w * nh / h), nh), Image.LANCZOS)
        left = (bg.width - w) // 2
        bg = bg.crop((left, 0, left + w, nh)).filter(ImageFilter.GaussianBlur(radius=max(30, w // 15)))
        bg = ImageEnhance.Brightness(bg).enhance(0.9)
    bg.paste(im, (0, (nh - h) // 2))
    return bg, True


def clean_listing_image(src: Path, dst: Path, pad: bool = True) -> dict:
    im = Image.open(src).convert("RGB")
    im, code = remove_supplier_code(im)
    padded = False
    if pad:
        im, padded = pad_to_portrait(im)
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, "JPEG", quality=92)
    return {"code_removed": code, "padded": padded}
