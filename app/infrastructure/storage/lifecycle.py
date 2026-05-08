"""OSS lifecycle policy spec loader + applier — Phase E5.

Lifecycle rules live in `app/config/retention.yaml` so they're operator-
editable without code changes. This module:

1. Parses the yaml into typed dataclasses.
2. Renders the same rules into Aliyun OSS lifecycle XML / SDK payload.
3. Provides an `apply` entrypoint that pushes rules to a bucket via the
   `oss2` SDK (when configured).

The applier is intentionally idempotent — running it twice doesn't double
rules; OSS replaces the lifecycle config wholesale per bucket.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).parent.parent.parent / "config" / "retention.yaml"


@dataclass(frozen=True)
class Transition:
    days: int
    storage_class: str  # "IA" | "Archive"


@dataclass(frozen=True)
class Rule:
    prefix: str
    transitions: tuple[Transition, ...] = ()
    expiration_days: int | None = None


@dataclass(frozen=True)
class BucketSpec:
    name: str
    purpose: str
    private: bool
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class RetentionSpec:
    version: str
    buckets: tuple[BucketSpec, ...]

    def by_name(self, name: str) -> BucketSpec | None:
        for b in self.buckets:
            if b.name == name:
                return b
        return None


def load(path: Path | None = None) -> RetentionSpec:
    """Parse the yaml. Raises ValueError on schema violation so misconfig
    fails at startup rather than silently dropping a rule."""
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("retention.yaml: top-level must be a mapping")
    version = str(raw.get("version") or "")
    buckets_raw = raw.get("buckets") or []
    if not isinstance(buckets_raw, list):
        raise ValueError("retention.yaml: `buckets` must be a list")

    buckets = tuple(_parse_bucket(b) for b in buckets_raw)
    seen_names = [b.name for b in buckets]
    if len(seen_names) != len(set(seen_names)):
        # A duplicate bucket entry would have one rule silently override the
        # other; force the operator to merge them by hand.
        raise ValueError(
            f"retention.yaml: duplicate bucket names: {seen_names}"
        )
    return RetentionSpec(version=version, buckets=buckets)


def _parse_bucket(raw: dict[str, Any]) -> BucketSpec:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("retention.yaml bucket missing `name`")
    rules_raw = raw.get("rules") or []
    if not isinstance(rules_raw, list):
        raise ValueError(f"retention.yaml {name}: `rules` must be a list")
    rules = tuple(_parse_rule(r, name) for r in rules_raw)
    return BucketSpec(
        name=name,
        purpose=str(raw.get("purpose") or ""),
        private=bool(raw.get("private", True)),
        rules=rules,
    )


def _parse_rule(raw: dict[str, Any], bucket_name: str) -> Rule:
    prefix = str(raw.get("prefix") or "")
    transitions_raw = raw.get("transitions") or []
    transitions: list[Transition] = []
    for t in transitions_raw:
        days = t.get("days")
        sc = t.get("storage_class")
        if not isinstance(days, int) or days < 1:
            raise ValueError(
                f"retention.yaml {bucket_name}/{prefix}: transition.days must be a positive int"
            )
        if sc not in ("IA", "Archive"):
            raise ValueError(
                f"retention.yaml {bucket_name}/{prefix}: storage_class must be IA|Archive"
            )
        transitions.append(Transition(days=days, storage_class=sc))
    expiration = raw.get("expiration_days")
    if expiration is not None and (not isinstance(expiration, int) or expiration < 1):
        raise ValueError(
            f"retention.yaml {bucket_name}/{prefix}: expiration_days must be a positive int"
        )
    return Rule(
        prefix=prefix,
        transitions=tuple(transitions),
        expiration_days=expiration,
    )


def to_oss_sdk_rules(spec: BucketSpec) -> list[dict[str, Any]]:
    """Render a bucket's rules into the dict shape `oss2.models.LifecycleRule`
    expects. Kept SDK-light here so unit tests don't need the SDK installed.

    Reference structure (matches oss2.models.LifecycleRule kwargs):
        {"id": "ai-r-0", "prefix": "", "status": "Enabled",
         "expiration": {"days": 30},
         "storage_transitions": [{"days": 30, "storage_class": "IA"}, ...]}
    """
    out: list[dict[str, Any]] = []
    for idx, rule in enumerate(spec.rules):
        rendered: dict[str, Any] = {
            "id": f"ai-r-{idx}",
            "prefix": rule.prefix,
            "status": "Enabled",
        }
        if rule.expiration_days is not None:
            rendered["expiration"] = {"days": rule.expiration_days}
        if rule.transitions:
            rendered["storage_transitions"] = [
                {"days": t.days, "storage_class": t.storage_class}
                for t in rule.transitions
            ]
        out.append(rendered)
    return out
