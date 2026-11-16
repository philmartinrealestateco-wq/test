"""Tool adapters used by the intel engine.

Each adapter wraps one external CLI, keeps its raw output formats out of the
pipeline and reports failure explicitly instead of returning an empty result.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def fold_handle(value: str | None) -> str:
    """Fold a username to letters and digits for comparison."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def canonical_url(url: str | None) -> str:
    """A comparison key for profile URLs.

    Sherlock and Maigret report the same profile with different casing and
    punctuation (``facebook.com/VitalikButerin`` vs
    ``facebook.com/vitalik.buterin``), so corroboration compares host, folded
    path and query rather than the raw string. Folding the path means two
    genuinely different accounts whose names differ only by ``.``/``-``/``_``
    would collide; that only ever raises a confidence label on a URL both tools
    already reported, never invents a profile.
    """
    if not url:
        return ""

    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[len("www.") :]
    path = fold_handle(parts.path)
    query = parts.query.lower()

    key = f"{host}/{path}" if path else host
    return f"{key}?{query}" if query else key
