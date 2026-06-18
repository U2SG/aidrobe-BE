"""LLM provider comparison — run with:  uv run python scripts/compare_llm.py

Sends the same question + system prompt to both Qwen and OpenAI and prints
results side by side. Reads keys from .env via python-dotenv so it works
without a running server.
"""

import asyncio
import os
import time

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
QUESTION = "今天适合穿什么？有什么搭配建议？"

# Minimal wardrobe context — replace with real items for a more realistic test.
SYSTEM_PROMPT = (
    "你是 Aidrobe 的穿搭助理。基于用户提供的衣橱结构化数据回答穿搭问题，"
    "建议要具体、可执行，只用中文回答。以下是用户的衣橱（已脱敏，仅含结构化属性）：\n"
    '{"user":"usr_test","items":['
    '{"category":"top","color_main":"white","status":"wearable","fit_status":"fits_well"},'
    '{"category":"bottom","color_main":"black","status":"wearable","fit_status":"fits_well"},'
    '{"category":"outerwear","color_main":"navy","status":"wearable","fit_status":"fits_well"},'
    '{"category":"shoes","color_main":"white","status":"wearable","fit_status":"fits_well"},'
    '{"category":"dress","color_main":"beige","status":"wearable","fit_status":"fits_well"}'
    "]}"
)


# ---------------------------------------------------------------------------
# Inline provider calls (no app bootstrap needed)
# ---------------------------------------------------------------------------
async def call_qwen(question: str, system_prompt: str) -> tuple[str, int, float]:
    import httpx

    api_key = os.getenv("QWEN_API_KEY", "")
    model = os.getenv("QWEN_LLM_MODEL", "qwen-turbo")
    base_url = os.getenv(
        "QWEN_LLM_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    )
    if not api_key:
        return "❌ QWEN_API_KEY 未配置", 0, 0.0

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        "temperature": 0.4,
    }
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        resp.raise_for_status()
    elapsed = time.monotonic() - t0
    data = resp.json()
    text = str(data["choices"][0]["message"]["content"])
    tokens = data.get("usage", {}).get("total_tokens", 0)
    return text, tokens, elapsed


async def call_openai(question: str, system_prompt: str) -> tuple[str, int, float]:
    import httpx

    api_key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("OPENAI_LLM_MODEL", "gpt-5.2")
    if not api_key:
        return "❌ OPENAI_API_KEY 未配置", 0, 0.0

    payload = {
        "model": model,
        "instructions": system_prompt,
        "input": question,
    }
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        resp.raise_for_status()
    elapsed = time.monotonic() - t0
    data = resp.json()

    # Extract text from output_text or output[].content[].text
    text = data.get("output_text") or ""
    if not text:
        for item in data.get("output", []):
            for c in item.get("content", []):
                if isinstance(c.get("text"), str):
                    text += c["text"]
    tokens = data.get("usage", {}).get("total_tokens", 0)
    return text.strip(), tokens, elapsed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    print(f"\n{'='*60}")
    print(f"问题: {QUESTION}")
    print(f"{'='*60}\n")

    qwen_task = asyncio.create_task(call_qwen(QUESTION, SYSTEM_PROMPT))
    openai_task = asyncio.create_task(call_openai(QUESTION, SYSTEM_PROMPT))

    qwen_text, qwen_tokens, qwen_time = await qwen_task
    openai_text, openai_tokens, openai_time = await openai_task

    print(f"【Qwen ({os.getenv('QWEN_LLM_MODEL','qwen-turbo')})】  {qwen_time:.1f}s  {qwen_tokens} tokens")
    print("-" * 60)
    print(qwen_text)

    print(f"\n【OpenAI ({os.getenv('OPENAI_LLM_MODEL','gpt-5.2')})】  {openai_time:.1f}s  {openai_tokens} tokens")
    print("-" * 60)
    print(openai_text)
    print()


if __name__ == "__main__":
    asyncio.run(main())
