"""gpt-image-2 quality/price sweep — low vs medium (vs the earlier high run).

Runs each sample at quality=low and quality=medium, capturing real token usage
+ latency per call so we can judge cost-effectiveness.

Run:
  uv run --with httpx --with python-dotenv --with pillow \
      python scripts/compare_openai_quality.py
"""

import base64
import io
import json
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
MODEL = "gpt-image-2"
QUALITIES = ["low", "medium"]

PROMPTS = {
    "top": "Isolate ONLY the top/shirt the person is wearing as a clean flat-lay e-commerce "
    "product photo on a pure solid white (#ffffff) background. Remove the person, phone, mirror "
    "and room. Preserve the exact color, sleeves, off-shoulder neckline and studs — do not restyle.",
    "dress": "Isolate ONLY the dress as a clean flat-lay e-commerce product photo on a pure solid "
    "white (#ffffff) background. Remove the person and background. Preserve the exact color, "
    "silhouette, straps and lace detail — do not restyle.",
    "shoes": "Isolate ONLY the pair of shoes as a clean e-commerce product photo on a pure solid "
    "white (#ffffff) background. Remove the bed and background. Preserve the exact color, suede "
    "texture and sole detail — do not restyle.",
}

# gpt-image-1 published rates (proxy until gpt-image-2 rates confirmed on the bill), $/1M tokens
RATE_IN_IMG = 10.0
RATE_IN_TXT = 5.0
RATE_OUT_IMG = 40.0


def call(category: str, path: str, quality: str):
    key = os.environ["OPENAI_API_KEY"]
    raw = open(path, "rb").read()
    files = {"image": (os.path.basename(path), raw, "image/jpeg")}
    data = {"model": MODEL, "prompt": PROMPTS[category], "size": "auto", "quality": quality, "n": "1"}
    t0 = time.monotonic()
    with httpx.Client(timeout=240.0) as c:
        r = c.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {key}"},
            files=files,
            data=data,
        )
    el = time.monotonic() - t0
    if r.status_code >= 400:
        return None, el, None, f"{r.status_code}: {r.text[:160]}"
    j = r.json()
    usage = j.get("usage", {})
    content = base64.b64decode(j["data"][0]["b64_json"])
    return content, el, usage, "ok"


def cost(u: dict) -> float:
    det = u.get("input_tokens_details", {})
    return (
        det.get("image_tokens", 0) * RATE_IN_IMG
        + det.get("text_tokens", 0) * RATE_IN_TXT
        + u.get("output_tokens", 0) * RATE_OUT_IMG
    ) / 1_000_000


def main() -> None:
    print(f"model={MODEL}  (cost $ est. via gpt-image-1 rates — confirm on bill)\n")
    summary = []
    for quality in QUALITIES:
        for category, path in INPUTS:
            name = os.path.splitext(os.path.basename(path))[0]
            content, el, usage, note = call(category, path, quality)
            if content:
                p = f"{OUT}/{name}__C_openai_{quality}.png"
                Image.open(io.BytesIO(content)).convert("RGB").save(p)
                out_tok = usage.get("output_tokens", 0)
                tot = usage.get("total_tokens", 0)
                c = cost(usage)
                print(f"[{quality:6}] {category:5}: {el:5.1f}s  out={out_tok:<5} total={tot:<5} ~${c:.3f}  -> {p}")
                summary.append((quality, category, el, out_tok, tot, c))
            else:
                print(f"[{quality:6}] {category:5}: {note}")
    print("\n=== avg per image ===")
    for q in QUALITIES:
        rows = [s for s in summary if s[0] == q]
        if rows:
            n = len(rows)
            print(
                f"  {q:6}: {sum(r[2] for r in rows)/n:5.1f}s  "
                f"out~{sum(r[3] for r in rows)//n}  ~${sum(r[5] for r in rows)/n:.3f}/img"
            )
    print("\nreference  high : ~170s  out~8232  ~$0.330/img")


if __name__ == "__main__":
    main()
