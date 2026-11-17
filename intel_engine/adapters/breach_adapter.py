"""Breach exposure adapter: one keyless email lookup against XposedOrNot.

``XposedOrNot().check_email`` is the free entry point and returns breach names
only. ``breach_analytics`` adds the year, record count, exposed data classes and
the per-breach verified flag, so a hit needs both calls. A clean email returns
no names and is never sent to analytics: XposedOrNot answers a clean lookup with
``ExposedBreaches: null``, which the client's model factory cannot parse.

A failed call stays an error. It must never be summarised as zero breaches.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote

PROVIDER = "XposedOrNot"
BREACH_PAGE = "https://xposedornot.com/breach/{name}"
DEFAULT_TIMEOUT = 30.0

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BreachError(RuntimeError):
    """The lookup produced no usable answer."""


class BreachConfigError(BreachError):
    """The XposedOrNot client is not installed."""


class BreachAPIError(BreachError):
    """XposedOrNot refused the request or returned an unusable payload."""


@dataclass(frozen=True)
class Breach:
    name: str
    title: str
    breach_date: str
    pwn_count: int | None
    data_classes: tuple[str, ...]
    is_verified: bool
    url: str | None = None


@dataclass
class BreachResult:
    email: str
    provider: str
    status: str  # "hit" or "clean"
    breaches: list[Breach] = field(default_factory=list)


def normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    if not _EMAIL.match(email):
        raise ValueError("email must look like name@example.com")
    return email


def mask_email(value: str | None) -> str:
    """Keep the domain, hide the local part, so a shared PDF does not leak it."""
    email = (value or "").strip()
    local, at, domain = email.partition("@")
    if not at or not local:
        return "***"
    return f"{local[:1]}***@{domain}"


def ensure_available() -> None:
    """Fail before prompting when the optional client is missing."""
    try:
        import xposedornot  # noqa: F401
    except ImportError as error:
        raise BreachConfigError(
            "xposedornot is not installed; run: pip install xposedornot"
        ) from error


def _client():
    ensure_available()
    from xposedornot import XposedOrNot

    return XposedOrNot(timeout=DEFAULT_TIMEOUT)


def _is_not_found(error: Exception) -> bool:
    try:
        from xposedornot import NotFoundError
    except ImportError:
        return False
    return isinstance(error, NotFoundError)


def _field(entry: object, name: str, default=None):
    """Read one field from a package dataclass or a raw API dict."""
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _flatten(value):
    for item in value:
        if isinstance(item, (list, tuple)):
            yield from _flatten(item)
        else:
            yield item


def _clean_names(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []

    names: list[str] = []
    seen: set[str] = set()
    for item in _flatten(value):
        name = str(item or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
    return names


def _split_classes(value: object) -> tuple[str, ...]:
    parts = [part.strip() for part in str(value or "").split(";")]
    return tuple(dict.fromkeys(part for part in parts if part))


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_verified(value: object) -> bool:
    """XposedOrNot sends "Yes"/"No"; bool("No") would be True."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"yes", "true", "1", "verified"}


def _breach_url(name: str) -> str:
    return BREACH_PAGE.format(name=quote(name, safe=""))


def _breach_from(name: str, entry: object | None) -> Breach:
    if entry is None:
        return Breach(
            name=name,
            title=name,
            breach_date="",
            pwn_count=None,
            data_classes=(),
            is_verified=False,
            url=_breach_url(name),
        )

    return Breach(
        name=name,
        title=name,
        breach_date=str(_field(entry, "xposed_date", "") or ""),
        pwn_count=_as_int(_field(entry, "xposed_records")),
        data_classes=_split_classes(_field(entry, "xposed_data", "")),
        is_verified=_is_verified(_field(entry, "verified", False)),
        url=_breach_url(name),
    )


def check_email(email: str, *, client=None) -> BreachResult:
    """Check one email and return hit or clean; raise for anything else."""
    normalized = normalize_email(email)

    if client is None:
        client = _client()

    try:
        response = client.check_email(normalized)
    except BreachError:
        raise
    except Exception as error:
        if _is_not_found(error):
            return BreachResult(
                email=normalized,
                provider=PROVIDER,
                status="clean",
            )
        raise BreachAPIError(f"XposedOrNot check_email failed: {error}") from error

    names = _clean_names(_field(response, "breaches", None))
    if not names:
        return BreachResult(
            email=normalized,
            provider=PROVIDER,
            status="clean",
        )

    try:
        analytics = client.breach_analytics(normalized)
    except BreachError:
        raise
    except Exception as error:
        raise BreachAPIError(
            f"XposedOrNot breach_analytics failed: {error}"
        ) from error

    details: dict[str, object] = {}
    for entry in _field(analytics, "breaches_details", None) or []:
        key = str(_field(entry, "breach", "") or "").strip().lower()
        if key:
            details[key] = entry

    breaches = [
        _breach_from(name, details.get(name.lower()))
        for name in names
    ]

    return BreachResult(
        email=normalized,
        provider=PROVIDER,
        status="hit",
        breaches=breaches,
    )


def encode_finding(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def decode_finding(text: str | None) -> dict | None:
    try:
        payload = json.loads(text or "")
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def finding_records(
    result: BreachResult,
    run_id: str,
) -> list[tuple[str, str | None, str]]:
    """(finding, url, confidence) rows for one lookup."""
    base = {
        "version": 1,
        "run_id": run_id,
        "provider": result.provider,
        "email": result.email,
    }

    if result.status == "clean":
        payload = {**base, "status": "clean"}
        return [(encode_finding(payload), None, "CLEAN")]

    if not result.breaches:
        raise BreachError("a hit result without breaches cannot be stored")

    records: list[tuple[str, str | None, str]] = []
    for breach in result.breaches:
        payload = {
            **base,
            "status": "hit",
            "name": breach.name,
            "title": breach.title,
            "date": breach.breach_date,
            "records": breach.pwn_count,
            "classes": list(breach.data_classes),
            "verified": breach.is_verified,
        }
        records.append(
            (
                encode_finding(payload),
                breach.url,
                "VERIFIED" if breach.is_verified else "UNVERIFIED",
            )
        )
    return records


def error_record(
    email: str,
    provider: str,
    detail: str,
    run_id: str,
) -> tuple[str, str | None, str]:
    payload = {
        "version": 1,
        "run_id": run_id,
        "provider": provider,
        "email": email,
        "status": "error",
        "detail": detail,
    }
    return encode_finding(payload), None, "ERROR"
