"""Shared read-path URL signing helper.

Internal-storage rows persist a stable `object_key`; reads materialize a
short-lived signed URL at request time (never persisting the TTL-bound URL).
External (import-flow) URLs have no key and pass through as-is.
"""

from __future__ import annotations


def resolve_render_url(object_key: str | None, fallback_url: str | None) -> str | None:
    """Sign and return a short-lived URL when an `object_key` is present;
    otherwise return the persisted URL as-is. If signing fails (storage
    misconfigured) fall through to the persisted URL rather than 500-ing
    the read."""
    if object_key:
        from app.bootstrap.container import object_storage

        try:
            return object_storage().sign_render_url(object_key)
        except Exception:
            pass
    return fallback_url
