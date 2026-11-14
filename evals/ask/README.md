# Ask (butler) live eval

20 questions (`questions.json`) run against a **running local backend with a real
LLM** and scored with deterministic checks. Not part of CI (needs a model key and
costs a few fen per run).

```bash
scripts/dev-up.sh                       # use AIDROBE_LLM_PROVIDER=qwen and AIDROBE_WEATHER_PROVIDER=qweather
                                        # enable ENABLE_ASK_AIDROBE and ENABLE_ASK_TOOLS in .env
uv run --python 3.12 python -m evals.ask.run --out /tmp/ask-eval.json
```

The runner copies the source user's wardrobe and settings (default: the dev user)
into a fresh eval user, so Ask history and learned preferences never touch the
source user, and marks the trap items unavailable.

| Check | Rule |
|---|---|
| `refs_valid` | every cited item belongs to the user and is wearable |
| `complete_outfit` | outfit questions cite (top + bottom, or dress) + shoes |
| `weather_grounded` | weather questions mention the actual condition or a temperature within ±3° |
| `trap_avoided` | an item marked unavailable is never cited |

Also recorded: latency, tokens, which tools were used.
