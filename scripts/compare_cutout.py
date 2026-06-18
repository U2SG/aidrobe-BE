"""Cutout comparison — rembg(u2net_cloth_seg) vs wanx(description_edit).

Runs both arms over the same real production garment photos and dumps
side-by-side results + metrics, so we can judge effect on data (not a synthetic).

Run:
  uv run --with rembg --with onnxruntime --with httpx --with python-dotenv \
      python scripts/compare_cutout.py

Arm A (rembg cloth_seg): garment segmentation, pixel-faithful, zero redraw.
  cloth_seg emits a 3-class mask stacked vertically (upper / lower / full-body);
  we pick the row matching the category and composite the original onto white.
  NOTE: cloth_seg has no shoe/accessory class — shoes are expected to fail here.

Arm B (wanx description_edit): instruction-guided global edit. Can produce a
  clean flat-lay but may redraw / hallucinate garment detail (neckline, studs).
"""

import asyncio
import base64
import io
import os
import time

from dotenv import load_dotenv

load_dotenv(dotenv_path="D:/project/Python/aidrobe/.env")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

INPUTS = [
    ("top", ".cutout-exp/in/d5a7fd26_top-selfie.jpg"),
    ("dress", ".cutout-exp/in/63e7de37_wardrobe-1781528482276.jpg"),
    ("shoes", ".cutout-exp/in/dd1be4aa_wardrobe-1781531506616.jpg"),
]
OUT = ".cutout-exp/out"
os.makedirs(OUT, exist_ok=True)

# cloth_seg stacks 3 masks top→bottom: upper-body, lower-body, full-body(dress)
CLASS_ROW = {"top": 0, "bottom": 1, "dress": 2}

WANX_PROMPTS = {
    "top": "提取图中人物穿着的上衣，单独平铺展示在纯白色背景上，电商商品图风格，"
    "去除人物、手机和房间背景，完整保留衣服原本的颜色、版型、领口和所有细节，不要改变款式",
    "dress": "提取图中的连衣裙，单独平铺展示在纯白色背景上，电商商品图风格，"
    "去除人物和背景，完整保留裙子原本的颜色、版型、蕾丝和所有细节，不要改变款式",
    "shoes": "提取图中的鞋子，单独平铺展示在纯白色背景上，电商商品图风格，"
    "去除床和背景，完整保留鞋子原本的颜色、材质和所有细节，不要改变款式",
}


def _data_uri(b: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(b).decode("ascii")


# ---------------------------------------------------------------------------
# Arm A: rembg u2net_cloth_seg
# ---------------------------------------------------------------------------
def arm_rembg(category: str, path: str) -> tuple[Image.Image, float, float, str]:
    from rembg import new_session, remove

    sess = new_session("u2net_cloth_seg")
    raw = open(path, "rb").read()
    t0 = time.monotonic()
    mask_png = remove(raw, session=sess, only_mask=True, post_process_mask=True)
    elapsed = time.monotonic() - t0

    mask = Image.open(io.BytesIO(mask_png)).convert("L")
    orig = Image.open(path).convert("RGB")
    W, H = orig.size
    mw, mh = mask.size

    note = ""
    # split the vertically-stacked 3-class mask and pick the category's row
    if mh >= H * 2.5:
        third = mh // 3
        row = CLASS_ROW.get(category, 2)
        mask = mask.crop((0, row * third, mw, (row + 1) * third))
        note = f"cloth_seg row {row} ({['upper','lower','full'][row]})"
    else:
        note = "single-channel mask"

    mask = mask.resize((W, H))
    arr = np.array(mask)
    frac = round(float((arr > 10).mean()), 4)

    white = Image.new("RGB", (W, H), (255, 255, 255))
    comp = Image.composite(orig, white, mask)
    bbox = mask.point(lambda p: 255 if p > 10 else 0).getbbox()
    if bbox:
        comp = comp.crop(bbox)
    return comp, elapsed, frac, note


# ---------------------------------------------------------------------------
# Arm B: wanx description_edit
# ---------------------------------------------------------------------------
async def arm_wanx(category: str, path: str) -> tuple[Image.Image | None, float, str]:
    key = os.environ.get("QWEN_API_KEY", "")
    if not key:
        return None, 0.0, "QWEN_API_KEY missing"
    model = os.getenv("QWEN_IMAGEEDIT_MODEL", "wanx2.1-imageedit")
    base = os.getenv(
        "QWEN_IMAGEEDIT_BASE_URL",
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2image/image-synthesis",
    )
    poll_base = os.getenv("QWEN_TASK_POLL_BASE_URL", "https://dashscope.aliyuncs.com/api/v1/tasks")
    raw = open(path, "rb").read()
    payload = {
        "model": model,
        "input": {
            "function": "description_edit",
            "prompt": WANX_PROMPTS[category],
            "base_image_url": _data_uri(raw),
        },
        "parameters": {"n": 1},
    }
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(
            base,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            },
            json=payload,
        )
        if r.status_code >= 400:
            return None, time.monotonic() - t0, f"create {r.status_code}: {r.text[:200]}"
        tid = r.json()["output"]["task_id"]
        url = None
        for _ in range(40):
            await asyncio.sleep(3)
            p = await c.get(f"{poll_base}/{tid}", headers={"Authorization": f"Bearer {key}"})
            p.raise_for_status()
            o = p.json()["output"]
            st = o.get("task_status")
            if st == "SUCCEEDED":
                results = o.get("results") or []
                if results and results[0].get("url"):
                    url = results[0]["url"]
                break
            if st == "FAILED":
                return None, time.monotonic() - t0, f"FAILED: {o.get('message') or o}"
        if not url:
            return None, time.monotonic() - t0, "no url / timeout"
        img = await c.get(url)
        img.raise_for_status()
    elapsed = time.monotonic() - t0
    return Image.open(io.BytesIO(img.content)).convert("RGB"), elapsed, "ok"


def _kb(img: Image.Image, fmt: str = "PNG") -> int:
    b = io.BytesIO()
    img.save(b, format=fmt)
    return len(b.getvalue()) // 1024


async def main() -> None:
    rows = []
    for category, path in INPUTS:
        name = os.path.splitext(os.path.basename(path))[0]
        print(f"\n{'='*64}\n[{category}] {path}\n{'='*64}")

        # Arm A
        try:
            a_img, a_t, a_frac, a_note = arm_rembg(category, path)
            a_path = f"{OUT}/{name}__A_rembg.png"
            a_img.save(a_path)
            print(f"  A rembg : {a_t:5.1f}s  mask_frac={a_frac:<7} {a_note}  -> {a_path}")
            a_cell = (a_path, f"{a_t:.1f}s · mask {a_frac} · {a_note}")
        except Exception as e:  # noqa: BLE001
            print(f"  A rembg : ERROR {e}")
            a_cell = (None, f"ERROR {e}")

        # Arm B
        try:
            b_img, b_t, b_note = await arm_wanx(category, path)
            if b_img is not None:
                b_path = f"{OUT}/{name}__B_wanx.png"
                b_img.save(b_path)
                print(f"  B wanx  : {b_t:5.1f}s  {b_note}  -> {b_path}")
                b_cell = (b_path, f"{b_t:.1f}s · {b_note}")
            else:
                print(f"  B wanx  : {b_t:5.1f}s  {b_note}")
                b_cell = (None, b_note)
        except Exception as e:  # noqa: BLE001
            print(f"  B wanx  : ERROR {e}")
            b_cell = (None, f"ERROR {e}")

        rows.append((category, path, a_cell, b_cell))

    # side-by-side HTML
    def rel(p: str | None) -> str:
        return os.path.basename(p) if p else ""

    html = [
        "<!doctype html><meta charset='utf-8'><title>cutout compare</title>",
        "<style>body{font-family:system-ui;background:#fafafa;color:#0a0a0a;margin:24px}",
        "table{border-collapse:collapse}td{border:1px solid #ddd;padding:8px;vertical-align:top;text-align:center}",
        "img{max-width:240px;max-height:320px;background:#fff;border:1px solid #eee}",
        "th{padding:8px}.cap{font-size:12px;color:#555;max-width:240px}</style>",
        "<h2>cutout compare — rembg(cloth_seg) vs wanx(description_edit)</h2>",
        "<table><tr><th>category</th><th>original</th><th>A · rembg cloth_seg</th><th>B · wanx description_edit</th></tr>",
    ]
    for category, path, a_cell, b_cell in rows:
        html.append(
            f"<tr><td><b>{category}</b></td>"
            f"<td><img src='../in/{os.path.basename(path)}'><div class='cap'>original</div></td>"
            f"<td>{'<img src=' + rel(a_cell[0]) + '>' if a_cell[0] else '—'}<div class='cap'>{a_cell[1]}</div></td>"
            f"<td>{'<img src=' + rel(b_cell[0]) + '>' if b_cell[0] else '—'}<div class='cap'>{b_cell[1]}</div></td></tr>"
        )
    html.append("</table>")
    with open(f"{OUT}/compare.html", "w", encoding="utf-8") as f:
        f.write("\n".join(html))
    print(f"\n=> open {OUT}/compare.html")


if __name__ == "__main__":
    asyncio.run(main())
