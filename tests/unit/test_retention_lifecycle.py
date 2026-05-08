"""Phase E5 — retention.yaml loader + OSS rule rendering."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.infrastructure.storage import lifecycle


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "retention.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_load_default_yaml_parses() -> None:
    """The shipping retention.yaml MUST parse with no errors. This test
    catches any misconfiguration committed to the repo."""
    spec = lifecycle.load()
    assert spec.version == "v1"
    # Plan §2.9 four-bucket layout, plus aidrobe-audit.
    names = [b.name for b in spec.buckets]
    assert {"aidrobe-items", "aidrobe-ootd", "aidrobe-private", "aidrobe-derived"} <= set(names)


def test_by_name_finds_bucket() -> None:
    spec = lifecycle.load()
    items = spec.by_name("aidrobe-items")
    assert items is not None
    assert items.private is True
    assert items.purpose


def test_private_bucket_has_30d_expiration() -> None:
    """The plan calls for try-on body images to auto-purge in 30 days; this
    is a regression guard so a yaml edit can't silently extend retention
    on the most-sensitive bucket."""
    spec = lifecycle.load()
    private = spec.by_name("aidrobe-private")
    assert private is not None
    assert any(r.expiration_days == 30 for r in private.rules)


def test_to_oss_sdk_rules_shape(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        version: "v1"
        buckets:
          - name: test-bucket
            private: true
            rules:
              - prefix: "user/"
                transitions:
                  - days: 30
                    storage_class: IA
                expiration_days: 365
        """,
    )
    spec = lifecycle.load(p)
    bucket = spec.by_name("test-bucket")
    assert bucket is not None
    rendered = lifecycle.to_oss_sdk_rules(bucket)
    assert rendered == [
        {
            "id": "ai-r-0",
            "prefix": "user/",
            "status": "Enabled",
            "expiration": {"days": 365},
            "storage_transitions": [{"days": 30, "storage_class": "IA"}],
        }
    ]


def test_load_rejects_duplicate_bucket_name(tmp_path: Path) -> None:
    """Two entries with the same `name` would silently override each
    other's rules; force a hard fail so the operator merges them."""
    p = _write(
        tmp_path,
        """
        version: "v1"
        buckets:
          - name: dup
            rules: []
          - name: dup
            rules: []
        """,
    )
    with pytest.raises(ValueError, match="duplicate bucket names"):
        lifecycle.load(p)


def test_load_rejects_invalid_storage_class(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        version: "v1"
        buckets:
          - name: x
            rules:
              - prefix: ""
                transitions:
                  - days: 30
                    storage_class: Glacier
        """,
    )
    with pytest.raises(ValueError, match="storage_class"):
        lifecycle.load(p)


def test_load_rejects_zero_days_transition(tmp_path: Path) -> None:
    """`days: 0` would mean "transition immediately on creation" which is
    almost certainly a typo for "never". Refuse rather than guess."""
    p = _write(
        tmp_path,
        """
        version: "v1"
        buckets:
          - name: x
            rules:
              - prefix: ""
                transitions:
                  - days: 0
                    storage_class: IA
        """,
    )
    with pytest.raises(ValueError, match="positive int"):
        lifecycle.load(p)
