"""V3-003 — the V3 purchase-decision surface is not reachable.

V3-003 ships persistence and the flag only; it registers no router. So this is
a *scope* assertion, not a proof that gating works: with no route to gate, it
would pass with the flag on too. The 503-when-off behaviour frozen in contract
§2 gets its real test when V3-004 / V3-007 / V3-008 add the endpoints.
"""

from app.config.settings import Settings
from app.main import create_app

V3_DECISION_PREFIXES = ("/v1/purchase-candidates", "/v1/wardrobe-decisions")


def test_flag_defaults_off() -> None:
    assert Settings().ENABLE_V3_PURCHASE_DECISION is False


def test_no_v3_purchase_decision_routes_are_registered() -> None:
    paths = {getattr(route, "path", "") for route in create_app().routes}

    assert not [path for path in paths if path.startswith(V3_DECISION_PREFIXES)]
