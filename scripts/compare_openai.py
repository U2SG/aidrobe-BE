"""OpenAI gpt-image cutout arm — generative garment extraction (non-Aliyun).

Uses the images/edits endpoint with a transparent background so the model
isolates the garment as a flat-lay. NOTE: generative — it REDRAWS the garment,
so judge fidelity (does the studded off-shoulder neckline survive?), not just
"is the background gone".

Run:
  uv run --with httpx --with python-dotenv --with pillow python scripts/compare_openai.py
"""

import base64
import io
import os
import time

from dotenv import load_dotenv

load_dotenv(dotenv_path="D:/project/Python/aidrobe/.env")

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

INPUTS = [
    ("top", ".cutout-exp/in/d5a7fd26_top-selfie.jpg"),
    ("dress", ".cutout-exp/in/63e7de37_wardrobe-1781528482276.jpg"),
    ("shoes", ".cutout-exp/in/dd1be4aa_wardrobe-1781531506616.jpg"),
]
OUT = ".cutout-exp/out"
os.makedirs(OUT, exist_ok=True)

MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")

PROMPTS = {
    "top": "Isolate ONLY the top/shirt the person is wearing and present it as a clean "
    "flat-lay e-commerce product photo on a fully transparent background. Remove the "
    "person, phone, mirror and room. Preserve the garment's exact color, shape, sleeves, "
    "off-shoulder neckline and any studs/details — do not restyle or invent details.",
    "dress": "Isolate ONLY the dress and present it as a clean flat-lay e-commerce product "
    "photo on a fully transparent background. Remove the person and background. Preserve the "
    "exact color, silhouette, straps and lace detail — do not restyle.",
    "shoes": "Isolate ONLY the pair of shoes and present them as a clean e-commerce product "
    "photo on a fully transparent background. Remove the bed and background. Preserve the exact "
    "color, suede texture and sole detail — do not restyle.",
}


def edit(category: str, path: str) -> tuple[bytes | None, float, str]:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return None, 0.0, "OPENAI_API_KEY missing"
    raw = open(path, "rb").read()
    files = {"image": (os.path.basename(path), raw, "image/jpeg")}
    data = {
        "model": MODEL,
        "prompt": PROMPTS[category] + " Place it on a pure solid white (#ffffff) background.",
        "size": "auto",
        "quality": "high",
        "n": "1",
    }
    t0 = time.monotonic()
    with httpx.Client(timeout=240.0) as c:
        r = c.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {key}"},
            files=files,
            data=data,
        )
    elapsed = time.monotonic() - t0
    if r.status_code >= 400:
        return None, elapsed, f"{r.status_code}: {r.text[:240]}"
    j = r.json()
    b64 = j["data"][0].get("b64_json")
    if not b64:
        return None, elapsed, f"no b64_json: {str(j)[:200]}"
    return base64.b64decode(b64), elapsed, "ok"


def main() -> None:
    print(f"model = {MODEL}")
    for category, path in INPUTS:
        name = os.path.splitext(os.path.basename(path))[0]
        print(f"\n{'='*64}\n[{category}] {path}\n{'='*64}")
        try:
            content, t, note = edit(category, path)
            if content:
                # composite the transparent PNG onto white so it's easy to view
                im = Image.open(io.BytesIO(content)).convert("RGBA")
                white = Image.new("RGBA", im.size, (255, 255, 255, 255))
                comp = Image.alpha_composite(white, im).convert("RGB")
                p_t = f"{OUT}/{name}__C_openai_transparent.png"
                p_w = f"{OUT}/{name}__C_openai.png"
                Image.open(io.BytesIO(content)).save(p_t)
                comp.save(p_w)
                print(f"  gpt-image: {t:5.1f}s  {note}  -> {p_w}")
            else:
                print(f"  gpt-image: {t:5.1f}s  {note}")
        except Exception as e:  # noqa: BLE001
            print(f"  gpt-image: ERROR {e}")


if __name__ == "__main__":
    main()
