"""Cutout model bake-off (non-Aliyun) — local rembg models, free, no API key.

Compares garment/object segmentation models on the same real production photos:
  - u2net_cloth_seg : garment-only (upper/lower/dress), no shoe class
  - isnet-general-use: general foreground seg (handles shoes/accessories)
  - birefnet-general : open-source SOTA matting, cleanest edges (~1GB model)

Run:
  uv run --with rembg --with onnxruntime --with numpy --with pillow \
      python scripts/compare_models.py
"""

import io
import os
import time

import numpy as np
from PIL import Image

INPUTS = [
    ("top", ".cutout-exp/in/d5a7fd26_top-selfie.jpg"),
    ("dress", ".cutout-exp/in/63e7de37_wardrobe-1781528482276.jpg"),
    ("shoes", ".cutout-exp/in/dd1be4aa_wardrobe-1781531506616.jpg"),
]
OUT = ".cutout-exp/out"
os.makedirs(OUT, exist_ok=True)

CLASS_ROW = {"top": 0, "bottom": 1, "dress": 2}
# generic foreground models (no class stacking)
GENERIC_MODELS = ["isnet-general-use", "birefnet-general"]


def _composite_white(orig: Image.Image, mask: Image.Image) -> Image.Image:
    W, H = orig.size
    mask = mask.resize((W, H)).convert("L")
    white = Image.new("RGB", (W, H), (255, 255, 255))
    comp = Image.composite(orig, white, mask)
    bbox = mask.point(lambda p: 255 if p > 10 else 0).getbbox()
    if bbox:
        comp = comp.crop(bbox)
    return comp


def run_cloth_seg(category: str, path: str):
    from rembg import new_session, remove

    sess = new_session("u2net_cloth_seg")
    raw = open(path, "rb").read()
    t0 = time.monotonic()
    mask_png = remove(raw, session=sess, only_mask=True, post_process_mask=True)
    elapsed = time.monotonic() - t0
    mask = Image.open(io.BytesIO(mask_png)).convert("L")
    orig = Image.open(path).convert("RGB")
    _, H = orig.size
    mw, mh = mask.size
    if mh >= H * 2.5:
        third = mh // 3
        row = CLASS_ROW.get(category, 2)
        mask = mask.crop((0, row * third, mw, (row + 1) * third))
    frac = round(float((np.array(mask.resize(orig.size)) > 10).mean()), 4)
    return _composite_white(orig, mask), elapsed, frac


def run_generic(model: str, path: str):
    from rembg import new_session, remove

    sess = new_session(model)
    raw = open(path, "rb").read()
    t0 = time.monotonic()
    mask_png = remove(raw, session=sess, only_mask=True, post_process_mask=True)
    elapsed = time.monotonic() - t0
    mask = Image.open(io.BytesIO(mask_png)).convert("L")
    orig = Image.open(path).convert("RGB")
    frac = round(float((np.array(mask.resize(orig.size)) > 10).mean()), 4)
    return _composite_white(orig, mask), elapsed, frac


def main() -> None:
    for category, path in INPUTS:
        name = os.path.splitext(os.path.basename(path))[0]
        print(f"\n{'='*64}\n[{category}] {path}\n{'='*64}")

        try:
            img, t, frac = run_cloth_seg(category, path)
            p = f"{OUT}/{name}__M_cloth_seg.png"
            img.save(p)
            print(f"  cloth_seg        : {t:5.1f}s  frac={frac:<7} -> {p}")
        except Exception as e:  # noqa: BLE001
            print(f"  cloth_seg        : ERROR {e}")

        for model in GENERIC_MODELS:
            try:
                img, t, frac = run_generic(model, path)
                p = f"{OUT}/{name}__M_{model}.png"
                img.save(p)
                print(f"  {model:17}: {t:5.1f}s  frac={frac:<7} -> {p}")
            except Exception as e:  # noqa: BLE001
                print(f"  {model:17}: ERROR {e}")


if __name__ == "__main__":
    main()
