"""Shared utilities for extracted handler modules."""

from aircp_storage import _sqlite_to_iso8601

_TS_FIELDS = (
    "created_at", "updated_at", "last_activity",
    "claimed_at", "completed_at", "last_pinged_at",
    "deadline_at", "closed_at",
)


def normalize_timestamps(items):
    """Convert SQLite timestamps to ISO8601+Z for browser UTC parsing.

    Extracted from AircpHandler._normalize_timestamps -- zero behavior change.
    """
    normalized = []
    for item in items:
        item2 = dict(item)
        for f in _TS_FIELDS:
            if item2.get(f):
                item2[f] = _sqlite_to_iso8601(item2[f])
        normalized.append(item2)
    return normalized
