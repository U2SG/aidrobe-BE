"""Deterministic + heuristic checkers for recommendation-quality evals (BE #50).

A fixture is a JSON file with this shape::

    {
      "name": "...",
      "wardrobe":         [ {"id": "...", "category": "...", ...}, ... ],
      "user_preferences": {"negative": [...], "positive": [...]},
      "request":          "free-text request (the user's ask)",
      "request_context":  {"weather": {...}, "occasion": "...", "focused_item_id": "..."},
      "recent_outfits":   [ {"item_ids": [...]}, ... ],   # optional wear history
      "output":           { ...TodayRecommendationResponse-shaped JSON... },
      "assertions":       [ {"type": "...", ...}, ... ]
    }

The ``output`` field is REQUIRED (it is the recommendation under test) and is an
extension of the issue's example shape, which only showed the inputs +
assertions. Because the suite must be deterministic and must never call a real
LLM in CI, every fixture carries a representative output that the checkers run
against — golden outputs for the "should pass" cases and deliberately regressed
outputs for the "should catch" cases.

Each checker returns a ``CheckResult``. ``ok=False`` results carry an actionable
message naming the constraint that regressed.

Layers (per the issue):
  1. Contract  — schema validity, item-id existence, no hallucinated refs,
                 required reasoning fields, diversity when history exists.
  2. Heuristic — generic copy, repeated phrases, min specificity,
                 weather/occasion keyword alignment, focused-item adherence,
                 negative-preference avoidance, preference reference.
  3. LLM judge — OFF by default; an inert stub. Never calls a real LLM.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.presentation.api.v1.schemas.recommendations import TodayRecommendationResponse

# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    constraint: str
    message: str

    @classmethod
    def passed(cls, constraint: str) -> CheckResult:
        return cls(ok=True, constraint=constraint, message="ok")

    @classmethod
    def failed(cls, constraint: str, message: str) -> CheckResult:
        return cls(ok=False, constraint=constraint, message=message)


# --------------------------------------------------------------------------- #
# Small helpers over the output shape
# --------------------------------------------------------------------------- #


def output_item_ids(output: dict[str, Any]) -> list[str]:
    """Every wardrobe item id referenced across all recommended outfits."""
    ids: list[str] = []
    for outfit in output.get("outfits", []):
        for item in outfit.get("items", []):
            item_id = item.get("item_id")
            if item_id is not None:
                ids.append(str(item_id))
    return ids


def _all_copy(output: dict[str, Any]) -> list[str]:
    """All free-text strings a human reads: guidance + per-outfit title/reason
    + reasoning texts."""
    chunks: list[str] = []
    guidance = output.get("guidance_text")
    if guidance:
        chunks.append(str(guidance))
    for outfit in output.get("outfits", []):
        for key in ("title", "reason"):
            val = outfit.get(key)
            if val:
                chunks.append(str(val))
        for reason in outfit.get("reasoning") or []:
            text = reason.get("text") if isinstance(reason, dict) else None
            if text:
                chunks.append(str(text))
    return chunks


def _wardrobe_by_id(wardrobe: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(it["id"]): it for it in wardrobe}


# --------------------------------------------------------------------------- #
# Layer 1 — contract / deterministic checks (always run for every fixture)
# --------------------------------------------------------------------------- #


def check_schema_valid(output: dict[str, Any]) -> CheckResult:
    """The output must decode as a TodayRecommendationResponse."""
    try:
        TodayRecommendationResponse.model_validate(output)
    except Exception as exc:  # noqa: BLE001 — surface the validation error verbatim
        return CheckResult.failed("schema_validity", f"output failed schema validation: {exc}")
    return CheckResult.passed("schema_validity")


def check_no_hallucinated_items(output: dict[str, Any], wardrobe: list[dict[str, Any]]) -> CheckResult:
    """Every referenced item id must exist in the provided wardrobe."""
    known = set(_wardrobe_by_id(wardrobe))
    referenced = output_item_ids(output)
    unknown = sorted({i for i in referenced if i not in known})
    if unknown:
        return CheckResult.failed(
            "no_hallucinated_items",
            f"recommendation references item ids not in the wardrobe: {unknown}",
        )
    return CheckResult.passed("no_hallucinated_items")


def check_reasoning_fields_present(output: dict[str, Any]) -> CheckResult:
    """When an outfit carries reasoning, each entry needs non-empty type+text."""
    for outfit in output.get("outfits", []):
        reasoning = outfit.get("reasoning")
        if reasoning is None:
            continue  # reasoning is feature-gated; absent is allowed.
        for idx, reason in enumerate(reasoning):
            if not isinstance(reason, dict) or not reason.get("type") or not reason.get("text"):
                return CheckResult.failed(
                    "reasoning_fields_present",
                    f"outfit {outfit.get('outfit_id')} reasoning[{idx}] missing type/text: {reason!r}",
                )
    return CheckResult.passed("reasoning_fields_present")


def check_recent_outfit_diversity(
    output: dict[str, Any], recent_outfits: list[dict[str, Any]]
) -> CheckResult:
    """When wear history exists, no recommended outfit may be item-identical to a
    recent one."""
    if not recent_outfits:
        return CheckResult.passed("recent_outfit_diversity")
    recent_sets = [frozenset(str(i) for i in r.get("item_ids", [])) for r in recent_outfits]
    for outfit in output.get("outfits", []):
        rec_set = frozenset(str(it.get("item_id")) for it in outfit.get("items", []))
        if rec_set and rec_set in recent_sets:
            return CheckResult.failed(
                "recent_outfit_diversity",
                f"outfit {outfit.get('outfit_id')} repeats a recent worn outfit (items={sorted(rec_set)})",
            )
    return CheckResult.passed("recent_outfit_diversity")


# --------------------------------------------------------------------------- #
# Layer 2 — heuristic checks
# --------------------------------------------------------------------------- #

# Generic / mock-like copy phrases that signal a regression to placeholder text.
# Mixed zh/en because copy is Chinese but mock fallbacks have leaked English.
_GENERIC_PHRASES = (
    "推荐穿搭",
    "为你推荐",
    "今日穿搭",
    "穿搭建议",
    "lorem ipsum",
    "placeholder",
    "your outfit",
    "recommended outfit",
    "outfit for today",
    "here is",
    "todo",
)


def check_generic_copy(output: dict[str, Any]) -> CheckResult:
    """Reason/guidance text must not be bare generic placeholder copy."""
    for chunk in _all_copy(output):
        low = chunk.strip().lower()
        for phrase in _GENERIC_PHRASES:
            # Flag only when the generic phrase IS essentially the whole string,
            # not merely contained — avoids false positives on richer copy.
            if low == phrase or low.replace(" ", "") == phrase.replace(" ", ""):
                return CheckResult.failed(
                    "generic_copy",
                    f"copy is generic/mock-like: {chunk!r}",
                )
    return CheckResult.passed("generic_copy")


def check_repeated_phrases(output: dict[str, Any]) -> CheckResult:
    """The same reason string must not be reused verbatim across outfits."""
    reasons = [str(o.get("reason")) for o in output.get("outfits", []) if o.get("reason")]
    if len(reasons) < 2:
        return CheckResult.passed("repeated_phrases")
    counts = Counter(reasons)
    dupes = [text for text, n in counts.items() if n > 1]
    if dupes:
        return CheckResult.failed(
            "repeated_phrases",
            f"identical reason copy reused across outfits: {dupes}",
        )
    return CheckResult.passed("repeated_phrases")


def check_min_specificity(output: dict[str, Any], min_len: int = 6) -> CheckResult:
    """Each outfit's reason must clear a minimum length (specificity floor)."""
    for outfit in output.get("outfits", []):
        reason = (outfit.get("reason") or "").strip()
        if reason and len(reason) < min_len:
            return CheckResult.failed(
                "min_specificity",
                f"outfit {outfit.get('outfit_id')} reason too short to be specific: {reason!r}",
            )
    return CheckResult.passed("min_specificity")


# --------------------------------------------------------------------------- #
# Assertion dispatch (per-fixture declared assertions)
# --------------------------------------------------------------------------- #


def _assert_must_include_item(fixture: dict[str, Any], spec: dict[str, Any]) -> CheckResult:
    item_id = str(spec["item_id"])
    if item_id in output_item_ids(fixture["output"]):
        return CheckResult.passed("must_include_item")
    return CheckResult.failed(
        "must_include_item",
        f"focused/required item {item_id!r} is absent from every recommended outfit",
    )


def _assert_must_not_include_item(fixture: dict[str, Any], spec: dict[str, Any]) -> CheckResult:
    item_id = str(spec["item_id"])
    if item_id not in output_item_ids(fixture["output"]):
        return CheckResult.passed("must_not_include_item")
    return CheckResult.failed(
        "must_not_include_item",
        f"item {item_id!r} should have been avoided but appears in a recommended outfit",
    )


def _assert_must_reference_preference(fixture: dict[str, Any], spec: dict[str, Any]) -> CheckResult:
    needle = str(spec["value"])
    haystack = "\n".join(_all_copy(fixture["output"]))
    if needle in haystack:
        return CheckResult.passed("must_reference_preference")
    return CheckResult.failed(
        "must_reference_preference",
        f"expected the copy to reference the preference {needle!r}; not found in any reason/guidance text",
    )


def _assert_copy_contains(fixture: dict[str, Any], spec: dict[str, Any]) -> CheckResult:
    """Weather/occasion keyword alignment: copy must mention at least one of the
    provided keywords (any-of)."""
    keywords = [str(k) for k in spec["keywords"]]
    label = str(spec.get("label", "keyword"))
    haystack = "\n".join(_all_copy(fixture["output"]))
    if any(k in haystack for k in keywords):
        return CheckResult.passed(f"copy_contains:{label}")
    return CheckResult.failed(
        f"copy_contains:{label}",
        f"copy does not mention any expected {label} keyword {keywords}; "
        "weather/occasion alignment likely regressed",
    )


def _assert_no_negative_preference_items(fixture: dict[str, Any], spec: dict[str, Any]) -> CheckResult:
    """No recommended item may carry an attribute the user said they dislike.

    `spec` declares the attribute key + disliked values, e.g.
    {"type": "no_attribute_value", "attribute": "fit", "values": ["oversized"]}.
    """
    attribute = str(spec["attribute"])
    bad_values = {str(v).lower() for v in spec["values"]}
    by_id = _wardrobe_by_id(fixture["wardrobe"])
    offenders: list[str] = []
    for item_id in output_item_ids(fixture["output"]):
        item = by_id.get(item_id)
        if item is None:
            continue
        if str(item.get(attribute, "")).lower() in bad_values:
            offenders.append(item_id)
    if offenders:
        return CheckResult.failed(
            "no_attribute_value",
            f"recommended items violate negative preference {attribute}∈{sorted(bad_values)}: {offenders}",
        )
    return CheckResult.passed("no_attribute_value")


_ASSERTIONS = {
    "must_include_item": _assert_must_include_item,
    "must_not_include_item": _assert_must_not_include_item,
    "must_reference_preference": _assert_must_reference_preference,
    "copy_contains": _assert_copy_contains,
    "no_attribute_value": _assert_no_negative_preference_items,
}


def run_assertion(fixture: dict[str, Any], spec: dict[str, Any]) -> CheckResult:
    kind = spec["type"]
    handler = _ASSERTIONS.get(kind)
    if handler is None:
        return CheckResult.failed(kind, f"unknown assertion type {kind!r}")
    return handler(fixture, spec)


# --------------------------------------------------------------------------- #
# Fixture-shape validation (typo guard)
# --------------------------------------------------------------------------- #
#
# A fixture's *inputs* are themselves a tiny schema. A typo in an assertion or a
# request_context key would silently weaken a scenario (e.g. ``"typ"`` instead of
# ``"type"`` makes an assertion a no-op, ``"focus_item_id"`` makes the context
# inert). This validator fails LOUDLY at load time naming the fixture + offending
# key instead of letting the scenario quietly pass. Data-driven so it extends
# alongside new assertion types: register the type in ``_ASSERTIONS`` and its
# required fields in ``_ASSERTION_REQUIRED_FIELDS``.


class FixtureSchemaError(ValueError):
    """Raised when a fixture's input shape is malformed (unknown/missing key,
    bad assertion). Message always names the fixture + offending key."""


# Top-level keys present in EVERY fixture (must exist).
_REQUIRED_TOP_LEVEL_KEYS = frozenset(
    {"name", "description", "wardrobe", "user_preferences", "request", "request_context", "output", "assertions"}
)
# Optional top-level keys (present in some fixtures). Union of the two below =
# the full allow-list; anything else is a typo.
_OPTIONAL_TOP_LEVEL_KEYS = frozenset({"recent_outfits", "expect_failures"})
_ALLOWED_TOP_LEVEL_KEYS = _REQUIRED_TOP_LEVEL_KEYS | _OPTIONAL_TOP_LEVEL_KEYS

# Allowed inner keys of request_context (per the module docstring). An unknown
# key here (e.g. a typo'd "focus_item_id") would silently disable a scenario.
_ALLOWED_REQUEST_CONTEXT_KEYS = frozenset({"weather", "occasion", "focused_item_id"})

# Required fields per assertion `type`, keyed identically to _ASSERTIONS. Fields
# the handler reads via spec[...] are required; spec.get(...) ones (e.g.
# copy_contains' optional "label") are NOT listed here.
_ASSERTION_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "must_include_item": ("item_id",),
    "must_not_include_item": ("item_id",),
    "must_reference_preference": ("value",),
    "copy_contains": ("keywords",),
    "no_attribute_value": ("attribute", "values"),
}

# Adding a handler without registering its required fields (or vice versa) is a
# bug — keep the two maps in lockstep so the typo guard stays exhaustive.
assert set(_ASSERTION_REQUIRED_FIELDS) == set(_ASSERTIONS), (
    "_ASSERTION_REQUIRED_FIELDS must register exactly the assertion types in _ASSERTIONS: "
    f"missing={sorted(set(_ASSERTIONS) - set(_ASSERTION_REQUIRED_FIELDS))}, "
    f"extra={sorted(set(_ASSERTION_REQUIRED_FIELDS) - set(_ASSERTIONS))}"
)


def validate_fixture(name: str, fixture: dict[str, Any]) -> None:
    """Validate a fixture's input shape, raising ``FixtureSchemaError`` (naming
    ``name`` + the offending key) on any malformed/typo'd key. Pure + importable
    so both the loader and the proving-test reuse it."""
    keys = set(fixture)
    missing = _REQUIRED_TOP_LEVEL_KEYS - keys
    if missing:
        raise FixtureSchemaError(f"fixture {name!r}: missing required top-level key(s): {sorted(missing)}")
    unknown = keys - _ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise FixtureSchemaError(
            f"fixture {name!r}: unknown top-level key(s) {sorted(unknown)} "
            f"(typo?); allowed: {sorted(_ALLOWED_TOP_LEVEL_KEYS)}"
        )

    request_context = fixture["request_context"]
    if not isinstance(request_context, dict):
        raise FixtureSchemaError(
            f"fixture {name!r}: request_context must be an object, got {type(request_context).__name__}"
        )
    bad_ctx = set(request_context) - _ALLOWED_REQUEST_CONTEXT_KEYS
    if bad_ctx:
        raise FixtureSchemaError(
            f"fixture {name!r}: unknown request_context key(s) {sorted(bad_ctx)} "
            f"(typo?); allowed: {sorted(_ALLOWED_REQUEST_CONTEXT_KEYS)}"
        )

    assertions = fixture["assertions"]
    if not isinstance(assertions, list):
        raise FixtureSchemaError(f"fixture {name!r}: assertions must be a list, got {type(assertions).__name__}")
    for idx, spec in enumerate(assertions):
        if not isinstance(spec, dict):
            raise FixtureSchemaError(f"fixture {name!r}: assertions[{idx}] must be an object, got {spec!r}")
        kind = spec.get("type")
        if not kind:
            raise FixtureSchemaError(f"fixture {name!r}: assertions[{idx}] missing 'type' key (typo?): {spec!r}")
        required = _ASSERTION_REQUIRED_FIELDS.get(kind)
        if required is None:
            raise FixtureSchemaError(
                f"fixture {name!r}: assertions[{idx}] has unknown assertion type {kind!r}; "
                f"allowed: {sorted(_ASSERTION_REQUIRED_FIELDS)}"
            )
        missing_fields = [f for f in required if f not in spec]
        if missing_fields:
            raise FixtureSchemaError(
                f"fixture {name!r}: assertions[{idx}] (type {kind!r}) missing required field(s) "
                f"{missing_fields}: {spec!r}"
            )


# --------------------------------------------------------------------------- #
# Top-level evaluation entry point
# --------------------------------------------------------------------------- #


def evaluate(fixture: dict[str, Any]) -> list[CheckResult]:
    """Run the always-on contract + heuristic layers, then the per-fixture
    declared assertions. Returns every result (passed and failed) so the test
    can report exactly which constraints regressed."""
    output = fixture["output"]
    wardrobe = fixture.get("wardrobe", [])
    recent = fixture.get("recent_outfits", [])

    results: list[CheckResult] = [
        check_schema_valid(output),
        check_no_hallucinated_items(output, wardrobe),
        check_reasoning_fields_present(output),
        check_recent_outfit_diversity(output, recent),
        check_generic_copy(output),
        check_repeated_phrases(output),
        check_min_specificity(output),
    ]
    for spec in fixture.get("assertions", []):
        results.append(run_assertion(fixture, spec))
    return results


# --------------------------------------------------------------------------- #
# Layer 3 — optional LLM judge (OFF by default; inert stub, never calls an LLM)
# --------------------------------------------------------------------------- #


def llm_judge(fixture: dict[str, Any], *, enabled: bool = False) -> list[CheckResult]:
    """Placeholder for a future soft-quality judge. No-op (returns []) when
    disabled (the default); raises NotImplementedError if enabled in v1 — it must
    NEVER call a real LLM in tests. Kept structurally separate from the
    deterministic layers on purpose."""
    if not enabled:
        return []
    raise NotImplementedError(
        "LLM-judge eval layer is not implemented in v1; deterministic checks only."
    )
