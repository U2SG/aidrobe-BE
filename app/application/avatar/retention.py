"""V3-011 — when avatar material becomes due for purge.

Pure functions. No DB, no IO, no clock of their own — every deadline is
derived from a timestamp the caller already holds, so the same inputs always
give the same answer and a test can assert the policy without a container.

**This module exists because of the V3-011 human gate**: "final retention
duration requires privacy approval; code must use configurable values". The
durations live in `Settings` (`AVATAR_SOURCE_RETENTION_DAYS`,
`AVATAR_RETIRED_VERSION_RETENTION_DAYS`) and are read here, at the one place
that decides what "expired" means. Nothing hard-codes a number, and nothing
stores a pre-computed absolute deadline on a row — so when privacy approval
fixes the real durations, changing the configured value changes the policy for
existing rows too, instead of leaving historical rows on a retention decision
that was never approved.

What this module deliberately does **not** do: delete anything. There is no
sweeper in P0. `AvatarLifecycle.delete_profile` (avatar_repo) handles the
user-initiated deletion path, which is immediate and not retention-driven.
"""

from datetime import UTC, datetime, timedelta

from app.config.settings import Settings

#: A retention setting of 0 or less means "no automatic purge" — the material
#: is kept until the user deletes it. Encoded explicitly so a misconfigured
#: `0` can never be read as "purge immediately", which would silently destroy
#: user data on a typo.
KEEP_INDEFINITELY: None = None


def _deadline(anchor: datetime, days: int) -> datetime | None:
    if days <= 0:
        return KEEP_INDEFINITELY
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    return anchor + timedelta(days=days)


def source_asset_purge_due_at(created_at: datetime, settings: Settings) -> datetime | None:
    """When a source photo's stored object becomes due for purge.

    Anchored on creation, not on consent: an un-consented candidate is the
    material we have the *least* right to keep, so declining must not extend
    its life beyond an accepted one's.
    """
    return _deadline(created_at, settings.AVATAR_SOURCE_RETENTION_DAYS)


def retired_version_purge_due_at(retired_at: datetime | None, settings: Settings) -> datetime | None:
    """When a retired avatar version's canonical assets become due for purge.

    `None` for a live version (`retired_at IS NULL`): the active avatar is
    never on a retention clock, only superseded ones are.
    """
    if retired_at is None:
        return KEEP_INDEFINITELY
    return _deadline(retired_at, settings.AVATAR_RETIRED_VERSION_RETENTION_DAYS)


def is_due(deadline: datetime | None, now: datetime) -> bool:
    """Whether a deadline has passed. `None` (keep indefinitely) is never due."""
    if deadline is None:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now >= deadline
