"""V3-016 — acceptance 1 and 4, asserted over the tree rather than argued.

Two separate claims, and they fail for different reasons:

- **Acceptance 1** — "Application layer depends only on a StaticTryOnPort". A
  module under `app/application/` that names a try-on adapter is a dependency
  whether the import is at module level or inside a function body, so the source
  is read as text and both are caught.
- **Acceptance 4** — "Vendor-specific fields do not leak into public API". The
  three provenance columns are internal by contract §6.4, so they must appear in
  neither the response model nor the published OpenAPI document.

Reading files rather than importing them keeps this container-free, so it runs
under `--noconftest` as local evidence. The companion narrative is
`.agent-artifacts/v3-016/vendor-leakage-review.md`.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from app.application.ports.static_tryon import PipelineIdentity
from app.presentation.api.v1.routers.outfit_visual import OutfitVisualAssetResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
APPLICATION_DIR = REPO_ROOT / "app" / "application"
PORT_FILE = APPLICATION_DIR / "ports" / "static_tryon.py"

#: The internal triple. Named from the dataclass so this list cannot fall out of
#: step with the port.
INTERNAL_PROVENANCE_FIELDS = tuple(PipelineIdentity.__dataclass_fields__)

#: `app.infrastructure.vision` is where provider adapters live. Every import of
#: one from the application layer is listed here by name, so a new one is a red
#: test rather than a quiet dependency.
#:
#: The single entry is V3-013's, and it is exactly the shape V3-016 refuses to
#: repeat — see `test_the_application_layer_never_imports_a_try_on_adapter`.
KNOWN_VISION_IMPORTS_FROM_APPLICATION = {
    (
        "app/application/avatar/prepare_avatar.py",
        "app.infrastructure.vision.fake_avatar_preparation",
    ),
}

#: Adapter modules a try-on caller might reach for. `fake_static_tryon` is the
#: only one that exists today; the vendor name is here so that adding an adapter
#: and wiring it above the infrastructure line fails immediately rather than at
#: the next leakage review.
TRYON_ADAPTER_MODULES = ("fake_static_tryon", "minimax")


def _imported_modules(path: Path) -> set[str]:
    """Every module this file imports, including inside function bodies.

    Parsed rather than grepped. A text search over the source cannot tell an
    import from a docstring — and these modules document the vendor boundary at
    length, so a grep for "minimax" matches the prose explaining why no vendor is
    reachable. `ast.walk` descends into function bodies, so the deferred-import
    trick is caught too, which is the whole point here.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _application_sources() -> list[Path]:
    return sorted(APPLICATION_DIR.rglob("*.py"))


def test_the_application_layer_never_imports_a_try_on_adapter() -> None:
    """Acceptance 1, as a property of the import graph.

    This is where V3-016 deliberately diverges from V3-013.
    `prepare_avatar.preparation_pipeline_identity()` imports
    `fake_avatar_preparation`'s constants *inside a function body* to rebuild the
    provider identity in the application layer — legal, and pinned by its own
    test, but it is exactly the shape this criterion rules out. So the try-on
    port reports its own identity and callers ask the port, which leaves nothing
    for a module here to import.
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()} -> {module}"
        for path in _application_sources()
        for module in _imported_modules(path)
        if any(marker in module for marker in TRYON_ADAPTER_MODULES)
    ]

    assert offenders == [], f"application layer reaches a try-on adapter: {offenders}"


def test_every_provider_adapter_reached_from_the_application_layer_is_declared() -> None:
    """Wider than try-on, so the boundary is measured rather than assumed.

    Scoped to `app.infrastructure.vision` — the provider-adapter package —
    rather than to `app.infrastructure` wholesale, because the existing
    sanitizer imports (`app.infrastructure.llm.sanitizer`, used by six
    application modules) are not provider adapters: bounding untrusted text
    before it reaches a model is the opposite of calling one. The same
    distinction `ports/avatar_preparation.py` draws.
    """
    found = {
        (path.relative_to(REPO_ROOT).as_posix(), module)
        for path in _application_sources()
        for module in _imported_modules(path)
        if module.startswith("app.infrastructure.vision")
    }

    assert found == KNOWN_VISION_IMPORTS_FROM_APPLICATION


def test_the_port_module_itself_imports_nothing_below_it() -> None:
    """The boundary is only real if the boundary file respects it.

    No infrastructure, no ORM, no imaging library: the port is types and
    docstrings, so nothing about a provider or a database can reach it.
    """
    imported = _imported_modules(PORT_FILE)
    offenders = [
        module
        for module in imported
        if module.startswith(("app.infrastructure", "app.presentation", "sqlalchemy", "PIL"))
    ]

    assert offenders == [], offenders


def test_the_public_asset_response_carries_no_provenance() -> None:
    """Acceptance 4 at the response model.

    `GET /v1/outfit-visual-assets/{asset_id}` is the M10 shape contract §6.2
    keeps "verbatim and only extended additively"; `provider` / `model_version` /
    `prompt_version` are internal columns and are not part of that extension.
    """
    exposed = set(OutfitVisualAssetResponse.model_fields)

    assert exposed.isdisjoint(INTERNAL_PROVENANCE_FIELDS), exposed


def test_the_published_openapi_document_carries_no_provenance() -> None:
    """The model is what the code returns; this is what FE is handed.

    Read from `docs/openapi.json` because that artifact — not the in-process
    schema — is what `./init.sh contract` byte-compares against the FE mirror,
    and therefore what a client generator consumes.
    """
    document = json.loads((REPO_ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    schema = document["components"]["schemas"]["OutfitVisualAssetResponse"]

    assert set(schema["properties"]).isdisjoint(INTERNAL_PROVENANCE_FIELDS), schema["properties"]


def test_no_response_schema_anywhere_exposes_the_provenance_triple() -> None:
    """Wider than the one endpoint, because a leak needs only one shape.

    V3-017 through V3-020 will add preview surfaces; a `provider` field on any of
    them is the same leak, and this fails on the day it is written rather than at
    the next review. Only the *combination* is rejected — a lone `provider`
    property on some unrelated schema would be a false positive, whereas all
    three together is unambiguously this triple.
    """
    document = json.loads((REPO_ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))

    offenders = [
        name
        for name, schema in document["components"]["schemas"].items()
        if set(INTERNAL_PROVENANCE_FIELDS) <= set(schema.get("properties", {}))
    ]

    assert offenders == [], f"schemas exposing the provider pipeline: {offenders}"
