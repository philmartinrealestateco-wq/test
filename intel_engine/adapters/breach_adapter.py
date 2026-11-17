"""Breach exposure adapter: one email lookup against a public breach API.

Only the Have I Been Pwned v3 ``breachedaccount`` endpoint is implemented. The
provider table is the seam for a second service (DeHashed, LeakCheck): each
provider gets its own request and parser, while the records written to
``osint_findings`` keep the same shape.

Three outcomes stay distinct: ``hit`` (breaches were returned), ``clean`` (the
API answered and knew no breach) and ``BreachAPIError`` (no usable answer).
A failed call must never be summarised as zero breaches.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import quote

import requests
from requests.exceptions import RequestException

HIBP_BASE = "https://haveibeenpwned.com/api/v3"
HIBP_BREACH_PAGE = "https://haveibeenpwned.com/PwnedWebsites#{name}"
PROVIDERS = {"hibp": "HIBP"}
DEFAULT_PROVIDER = "hibp"
DEFAULT_TIMEOUT = 20
USER_AGENT = "intel_engine-self-audit/1.0"

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BreachError(RuntimeError):
    """The lookup produced no usable answer."""


class BreachConfigError(BreachError):
    """The environment is missing the key or names an unknown provider."""


class BreachAPIError(BreachError):
    """The breach API refused the request or answered with something unusable."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        retry_after: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Breach:
    name: str
    title: str
    breach_date: str
    pwn_count: int | None
    data_classes: tuple[str, ...]
    is_verified: bool
    url: str


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


def provider_from_env(environ: Mapping[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    name = (source.get("BREACH_API_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if name not in PROVIDERS:
        raise BreachConfigError(
            f"unknown BREACH_API_PROVIDER {name!r}; supported: {', '.join(sorted(PROVIDERS))}"
        )
    return name


def api_key_from_env(environ: Mapping[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    key = (source.get("BREACH_API_KEY") or "").strip()
    if not key:
        raise BreachConfigError("BREACH_API_KEY is not set")
    return key


def lookup(
    email: str,
    api_key: str,
    *,
    provider: str = DEFAULT_PROVIDER,
    session=None,
    timeout: int = DEFAULT_TIMEOUT,
    base_url: str = HIBP_BASE,
) -> BreachResult:
    """Check one email and return hit or clean; raise for anything else."""
    normalized = normalize_email(email)
    if provider not in PROVIDERS:
        raise BreachConfigError(f"unknown provider {provider!r}")
    return _lookup_hibp(normalized, api_key, session, timeout, base_url)


def _lookup_hibp(
    email: str,
    api_key: str,
    session,
    timeout: int,
    base_url: str,
) -> BreachResult:
    client = requests if session is None else session
    url = f"{base_url}/breachedaccount/{quote(email, safe='')}"

    try:
        response = client.get(
            url,
            params={"truncateResponse": "false"},
            headers={
                "accept": "application/json",
                "hibp-api-key": api_key,
                "user-agent": USER_AGENT,
            },
            timeout=timeout,
        )
    except RequestException as error:
        raise BreachAPIError(f"the request failed: {error}") from error

    if response.status_code == 404:
        return BreachResult(email=email, provider=PROVIDERS["hibp"], status="clean")

    if response.status_code == 401:
        raise BreachAPIError("HTTP 401: the API rejected BREACH_API_KEY", status=401)

    if response.status_code == 429:
        retry = response.headers.get("Retry-After")
        suffix = f"; retry after {retry}s" if retry else ""
        raise BreachAPIError(
            f"HTTP 429: rate limited{suffix}",
            status=429,
            retry_after=retry,
        )

    if response.status_code != 200:
        detail = (response.text or "").strip()[:200]
        raise BreachAPIError(
            f"HTTP {response.status_code}: {detail or 'no detail'}",
            status=response.status_code,
        )

    try:
        payload = response.json()
    except ValueError as error:
        raise BreachAPIError(f"the API returned invalid JSON: {error}") from error

    if not isinstance(payload, list):
        raise BreachAPIError("the API returned an unexpected payload (expected a list)")

    breaches = _parse_hibp(payload)
    if not breaches:
        raise BreachAPIError(
            f"the API returned {len(payload)} hit(s) but none could be parsed"
        )

    return BreachResult(
        email=email,
        provider=PROVIDERS["hibp"],
        status="hit",
        breaches=breaches,
    )


def _parse_hibp(payload: list) -> list[Breach]:
    breaches: list[Breach] = []
    for item in payload:
        if not isinstance(item, dict):
            continue

        name = str(item.get("Name") or item.get("Title") or "").strip()
        if not name:
            continue

        classes = item.get("DataClasses")
        count = item.get("PwnCount")

        breaches.append(
            Breach(
                name=name,
                title=str(item.get("Title") or name),
                breach_date=str(item.get("BreachDate") or ""),
                pwn_count=count if isinstance(count, int) else None,
                data_classes=(
                    tuple(str(entry) for entry in classes)
                    if isinstance(classes, list)
                    else ()
                ),
                is_verified=bool(item.get("IsVerified")),
                url=HIBP_BREACH_PAGE.format(name=quote(name, safe="")),
            )
        )

    return breaches


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
