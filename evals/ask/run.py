"""Live Ask eval (butler P0 baseline; see questions.json).

Runs every question against a running local backend with a real LLM and scores
the answers with deterministic checks. It copies a source user's wardrobe and
settings into a fresh eval user (so Ask history / learned preferences never
touch the source user), then marks the trap items unavailable.

    uv run python -m evals.ask.run --source-user <uuid> --out /tmp/ask-eval.json

Costs real LLM calls (~20 questions × a few hundred tokens). Not part of CI.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE_USER = "00000000-0000-4000-8000-000000000001"


def psql(container: str, sql: str) -> str:
    out = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-U", "aidrobe", "-d", "aidrobe", "-tA", "-v", "ON_ERROR_STOP=1"],
        input=sql,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def columns(container: str, table: str) -> list[str]:
    rows = psql(
        container,
        f"select column_name from information_schema.columns where table_name = '{table}' order by ordinal_position;",
    )
    return [r for r in rows.splitlines() if r]


def make_eval_user(container: str, source: str, unavailable_titles: list[str]) -> str:
    """Copy `source`'s live wardrobe items and settings into a new user."""
    user = str(uuid.uuid4())
    item_cols = [c for c in columns(container, "wardrobe_items") if c not in ("id", "user_id")]
    settings_cols = [c for c in columns(container, "user_settings") if c not in ("id", "user_id")]
    items = ", ".join(item_cols)
    setts = ", ".join(settings_cols)
    titles = ", ".join("'" + t.replace("'", "''") + "'" for t in unavailable_titles) or "''"
    psql(
        container,
        f"""
        insert into users (id) values ('{user}');
        insert into wardrobe_items (id, user_id, {items})
          select gen_random_uuid(), '{user}', {items} from wardrobe_items
          where user_id = '{source}' and deleted_at is null;
        insert into user_settings (id, user_id, {setts})
          select gen_random_uuid(), '{user}', {setts} from user_settings where user_id = '{source}';
        update wardrobe_items set status = 'unavailable' where user_id = '{user}' and title in ({titles});
        """,
    )
    return user


def unwrap(body: Any) -> Any:
    return body["data"] if isinstance(body, dict) and "code" in body and "data" in body else body


@dataclass
class Score:
    id: str
    kind: str
    question: str
    status: int
    seconds: float
    tokens: int = 0
    answer: str = ""
    refs: list[str] = field(default_factory=list)
    used: list[str] = field(default_factory=list)
    refs_valid: bool | None = None
    complete_outfit: bool | None = None
    weather_grounded: bool | None = None
    trap_avoided: bool | None = None


def complete(categories: set[str]) -> bool:
    return ({"top", "bottom"} <= categories or "dress" in categories) and "shoes" in categories


def grounded(answer: str, weather: dict[str, Any] | None) -> bool:
    if not weather:
        return False
    condition = str(weather.get("condition") or "")
    if condition and condition in answer:
        return True
    actual = weather.get("temperature_c")
    if actual is None:
        return False
    for number in re.findall(r"(-?\d{1,2})\s*(?:°|度|℃)", answer):
        if abs(int(number) - float(actual)) <= 3:
            return True
    return False


def run(args: argparse.Namespace) -> int:
    spec = json.loads((HERE / "questions.json").read_text())
    user = make_eval_user(args.pg_container, args.source_user, spec["unavailable_titles"])
    client = httpx.Client(base_url=args.api, headers={"X-User-Id": user}, timeout=90)

    items = unwrap(client.get("/v1/wardrobe-items", params={"limit": 100}).json())["items"]
    by_id = {i["item_id"]: i for i in items}
    wearable = {i["item_id"] for i in items if i["status"] == "wearable" and i["fit_status"] != "no_longer_fits"}
    by_title = {i["title"]: i["item_id"] for i in items}

    weather: dict[str, dict[str, Any] | None] = {}
    for day, target in (("today", date.today()), ("tomorrow", date.today() + timedelta(days=1))):
        body = unwrap(client.get("/v1/recommendations/today", params={"target_date": target.isoformat()}).json())
        weather[day] = ((body or {}).get("context") or {}).get("weather")

    scores: list[Score] = []
    for q in spec["questions"]:
        started = time.perf_counter()
        response = client.post("/v1/ask", json={"question": q["question"]})
        if response.status_code == 429:
            time.sleep(30)
            started = time.perf_counter()
            response = client.post("/v1/ask", json={"question": q["question"]})
        seconds = time.perf_counter() - started
        score = Score(q["id"], q["kind"], q["question"], response.status_code, round(seconds, 2))
        if response.status_code == 200:
            data = unwrap(response.json())
            score.tokens = int(data.get("tokens_used") or 0)
            score.answer = data.get("answer") or ""
            score.refs = list(data.get("item_refs") or [])
            score.used = [u.get("summary", "") for u in data.get("used") or []]
            score.refs_valid = all(ref in wearable for ref in score.refs)
            if q.get("outfit"):
                score.complete_outfit = complete({by_id[r]["category"] for r in score.refs if r in by_id})
            if q["kind"] == "weather":
                score.weather_grounded = grounded(score.answer, weather.get(q.get("day", "today")))
            if q.get("trap"):
                score.trap_avoided = by_title.get(q["trap"]) not in score.refs
        scores.append(score)
        print(
            f"{q['id']:>3} {response.status_code} {seconds:5.1f}s refs={len(score.refs)} {score.answer[:50]}",
            flush=True,
        )
        time.sleep(args.pause)

    def rate(attr: str) -> str:
        values = [getattr(s, attr) for s in scores if getattr(s, attr) is not None]
        return f"{sum(values)}/{len(values)}" if values else "n/a"

    ok = [s for s in scores if s.status == 200]
    summary = {
        "eval_user": user,
        "answered": f"{len(ok)}/{len(scores)}",
        "refs_valid": rate("refs_valid"),
        "complete_outfit": rate("complete_outfit"),
        "weather_grounded": rate("weather_grounded"),
        "trap_avoided": rate("trap_avoided"),
        "used_any_tool": f"{sum(1 for s in ok if s.used)}/{len(ok)}",
        "median_seconds": sorted(s.seconds for s in ok)[len(ok) // 2] if ok else None,
        "total_tokens": sum(s.tokens for s in ok),
        "weather": weather,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.out:
        Path(args.out).write_text(
            json.dumps({"summary": summary, "scores": [asdict(s) for s in scores]}, ensure_ascii=False, indent=1)
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--source-user", default=DEFAULT_SOURCE_USER)
    parser.add_argument("--pg-container", default="aidrobe-postgres-1")
    parser.add_argument("--pause", type=float, default=1.0, help="seconds between questions (rate limit)")
    parser.add_argument("--out")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
