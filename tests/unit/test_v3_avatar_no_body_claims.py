"""V3-013 acceptance 4 — "No API claims sizing or biometric accuracy."

Asserted against the **generated OpenAPI document**, not against the Pydantic
classes, because the document is what a client actually reads and what FE
generates types from. A field could also arrive through a nested model or an
inherited base without any avatar schema file changing.

Contract §7 and V3_SPEC §15 are the source of the denylist: no size or fit
claims, and no body-shape, age, attractiveness or "fix" language of any kind.
V3-012 expressed the same rule one layer down as a type with no field to write
an inference into (`ports/avatar_detection.py`); this is the surface-level half
of the same guarantee.

Container-free: builds the app and reads its schema, touching no database.
"""

from __future__ import annotations

from typing import Any

import pytest

#: Substrings that must not appear in any property name on an avatar surface.
#: Substrings rather than exact names so `estimated_size`, `size_label` and
#: `sizeHint` are all caught — a denylist of exact names is one rename away from
#: useless.
FORBIDDEN_PROPERTY_FRAGMENTS = (
    "size",
    "fit_score",
    "measurement",
    "bust",
    "waist",
    "hip",
    "inseam",
    "shoulder_width",
    "weight",
    "height_cm",
    "bmi",
    "body_type",
    "body_shape",
    "figure",
    "age",
    "attractive",
    "beauty",
    "slim",
    "flatter",
    "skin_tone",
    "gender",
)


@pytest.fixture(scope="module")
def openapi_document() -> dict[str, Any]:
    from app.main import create_app

    return dict(create_app().openapi())


def _avatar_paths(document: dict[str, Any]) -> dict[str, Any]:
    return {
        path: spec
        for path, spec in document.get("paths", {}).items()
        if "avatar" in path
    }


def test_the_six_frozen_avatar_surfaces_are_all_present(
    openapi_document: dict[str, Any],
) -> None:
    """Contract §4.2's table, verbatim. Also the guard for the tests below.

    Without this, every denylist assertion would pass trivially if the router
    stopped being registered — a green suite over an absent API.
    """
    paths = _avatar_paths(openapi_document)

    assert set(paths) == {
        "/v1/avatar-profile",
        "/v1/avatar-candidates/{source_asset_id}/consent",
        "/v1/avatar-profile/uploads",
        "/v1/avatar-profile/prepare",
        "/v1/avatar-profile/replace",
    }
    assert set(paths["/v1/avatar-profile"]) == {"get", "delete"}


def _referenced_schema_names(node: Any, found: set[str]) -> None:
    """Collect every `#/components/schemas/X` reachable from a node."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _referenced_schema_names(value, found)
    elif isinstance(node, list):
        for value in node:
            _referenced_schema_names(value, found)


def _avatar_schemas(document: dict[str, Any]) -> dict[str, Any]:
    """Every schema reachable from an avatar path, transitively.

    Transitive because a top-level avatar response could nest a shared model, and
    a body claim smuggled in through a nested schema is still a body claim on the
    avatar API.
    """
    all_schemas = document.get("components", {}).get("schemas", {})
    names: set[str] = set()
    _referenced_schema_names(_avatar_paths(document), names)

    seen: set[str] = set()
    while names - seen:
        name = next(iter(names - seen))
        seen.add(name)
        nested: set[str] = set()
        _referenced_schema_names(all_schemas.get(name, {}), nested)
        names |= nested

    return {name: all_schemas[name] for name in seen if name in all_schemas}


def test_avatar_api_makes_no_body_claims(openapi_document: dict[str, Any]) -> None:
    """No property on any avatar schema names a body measurement or judgement."""
    schemas = _avatar_schemas(openapi_document)
    assert schemas, "no avatar schemas resolved — the walk is not looking at anything"

    offenders: list[str] = []
    for schema_name, schema in schemas.items():
        for property_name in schema.get("properties", {}):
            lowered = property_name.lower()
            for fragment in FORBIDDEN_PROPERTY_FRAGMENTS:
                if fragment in lowered:
                    offenders.append(f"{schema_name}.{property_name} (matched {fragment!r})")

    assert not offenders, (
        "avatar API exposes body/size properties, which contract §7 and V3_SPEC "
        f"§15 forbid: {offenders}"
    )


def test_avatar_query_and_path_parameters_make_no_body_claims(
    openapi_document: dict[str, Any],
) -> None:
    """The same rule for inputs.

    An endpoint that *accepts* a measurement is asking the user to believe we use
    it, which is the same claim from the other direction — and V3_SPEC §7.4 is
    explicit that P0 requires no 三围, weight or height.
    """
    offenders: list[str] = []
    for path, spec in _avatar_paths(openapi_document).items():
        for method, operation in spec.items():
            for parameter in operation.get("parameters", []):
                name = str(parameter.get("name", "")).lower()
                for fragment in FORBIDDEN_PROPERTY_FRAGMENTS:
                    if fragment in name:
                        offenders.append(f"{method.upper()} {path} ?{name}")

    assert not offenders, f"avatar API accepts body/size parameters: {offenders}"


def test_purge_derived_is_a_required_parameter(openapi_document: dict[str, Any]) -> None:
    """Contract §4.2: `purge_derived` is **required** (no default).

    Not a body-claim assertion, but it belongs on the generated document rather
    than in a behavioral test: "required with no default" is a statement about the
    published contract, and a default silently added later would be invisible to
    any test that just calls the endpoint with the parameter set.
    """
    delete_op = _avatar_paths(openapi_document)["/v1/avatar-profile"]["delete"]
    purge = next(p for p in delete_op["parameters"] if p["name"] == "purge_derived")

    assert purge["required"] is True
    assert purge["in"] == "query"
    assert "default" not in purge["schema"]


def test_provider_details_never_reach_a_response(openapi_document: dict[str, Any]) -> None:
    """Contract §6.4: `provider` / `model_version` / `prompt_version` are internal.

    They are columns on `avatar_versions` and values in a job payload, and they
    must not leak into the API — "Providers ... never leak into the domain
    contract or API responses".
    """
    internal_only = ("provider", "model_version", "prompt_version", "pipeline_identity")
    offenders = [
        f"{schema_name}.{property_name}"
        for schema_name, schema in _avatar_schemas(openapi_document).items()
        for property_name in schema.get("properties", {})
        if property_name.lower() in internal_only
    ]

    assert not offenders, f"provider internals exposed on the avatar API: {offenders}"
