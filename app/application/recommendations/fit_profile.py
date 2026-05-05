"""Compatibility import path for fit profile learning."""

from app.application.fit_profile.build_fit_profile import (
    LOOSE_SIGNALS,
    MIN_FEEDBACK_FOR_PREFERENCE,
    TIGHT_SIGNALS,
    aggregate_fit_profile,
    build_all_fit_profiles,
    build_fit_profile,
)

__all__ = [
    "LOOSE_SIGNALS",
    "MIN_FEEDBACK_FOR_PREFERENCE",
    "TIGHT_SIGNALS",
    "aggregate_fit_profile",
    "build_all_fit_profiles",
    "build_fit_profile",
]
