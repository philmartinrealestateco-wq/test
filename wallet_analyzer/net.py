"""HTTP transport: shared session, per-host rate limiting, retries, secret-safe errors.

Two rules this module enforces:

* every host gets its own limiter, shared across worker threads, so raising
  ``MAX_WORKERS`` never raises the request rate a provider sees;
* no exception message may contain a query string, because the Etherscan key
  travels as the ``apikey`` parameter.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

LOG = logging.getLogger(__name__)

USER_AGENT = "wallet-analyzer/1.0 (read-only public blockchain data)"
_SECRET_PARAM = re.compile(
    r"\b(apikey|api_key|api-key|access_token|token|key|api_code|auth|code)=[^&\s'\"]+",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s'\"]+", re.IGNORECASE)
# A provider key is often not in a URL query at all: Helius/QuickNode hand out
# JWTs, and some portals use bare UUIDs.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "throttl",
    "max calls per sec",
)


def _rate_limit_detail(payload: object) -> str | None:
    """Return throttling text when a successful HTTP body reports a rate limit.

    Etherscan signals per-second throttling as HTTP 200 with
    ``{"status": "0", "message": "NOTOK", "result": "Max calls per sec ..."}``.
    ``NOTOK`` alone is deliberately not enough: the same envelope carries
    invalid-key and plan errors, which must fail fast instead of being retried.
    """

    if not isinstance(payload, dict):
        return None

    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if code in (429, "429"):
            return str(error.get("message") or "HTTP 429")
        candidates = [error["message"]] if isinstance(error.get("message"), str) else []
    elif isinstance(error, str):
        candidates = [error]
    else:
        candidates = []

    if payload.get("code") in (429, "429"):
        return str(payload.get("message") or "HTTP 429")

    status = str(payload.get("status", ""))
    envelope_message = payload.get("message")
    if status == "0" or (
        isinstance(envelope_message, str) and envelope_message.upper() == "NOTOK"
    ):
        if isinstance(envelope_message, str):
            candidates.append(envelope_message)
        result = payload.get("result")
        if isinstance(result, str):
            candidates.append(result)

    text = " ".join(candidates).strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return text
    return None


def scrub(text: str) -> str:
    """Strip anything that looks like a credential from a message.

    Order matters: opaque tokens are removed *before* URL handling, because a
    bad URL can end up quoted raw ("Invalid URL 'eyJ...'") and, once lowercased
    into a hostname by the URL pass, would no longer match a token pattern.
    Full URLs are then collapsed to their host, because a token can also live in
    the path where a query-parameter filter would never see it.
    """

    without_tokens = _UUID.sub("[redacted-uuid]", _JWT.sub("[redacted-token]", text))

    def redact_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        scheme = "https" if raw.lower().startswith("https") else "http"
        try:
            host = urlparse(raw).hostname
        except ValueError:
            # urlparse raises on things like "https://[redacted-token]/" because
            # brackets imply an IPv6 literal. Scrubbing must never raise.
            host = None
        return f"{scheme}://{host or 'unknown-host'}/[redacted]"

    without_urls = _URL.sub(redact_url, without_tokens)
    return _SECRET_PARAM.sub(lambda match: f"{match.group(1)}=***", without_urls)


#: A Retry-After beyond this is a ban, not pacing: retrying inside the run only
#: wastes the wait, while another host may still answer.
MAX_RETRY_AFTER = 120.0


def _retry_after_is_long(value: str | None) -> bool:
    if not value:
        return False
    try:
        return float(value) > MAX_RETRY_AFTER
    except ValueError:
        # HTTP-date form; normal backoff is the right handling.
        return False


class ProviderError(RuntimeError):
    """A data source could not answer. Always safe to show to the user."""

    def __init__(
        self,
        message: str,
        *,
        host: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(scrub(message))
        self.host = host
        self.status = status


@dataclass(frozen=True)
class HostPolicy:
    min_interval: float
    max_attempts: int = 4


# Deliberately conservative: these are the *sustained* intervals we allow.
_POLICIES: dict[str, HostPolicy] = {
    "api.etherscan.io": HostPolicy(0.25),
    "eth.blockscout.com": HostPolicy(0.25),
    "blockchain.info": HostPolicy(1.0),
    "blockstream.info": HostPolicy(0.4),
    "api.mainnet-beta.solana.com": HostPolicy(0.7),
    "api.coingecko.com": HostPolicy(2.0),
    "api.coinbase.com": HostPolicy(0.4),
    "bsc-dataseed.binance.org": HostPolicy(0.3),
}
_DEFAULT_POLICY = HostPolicy(0.5)


def policy_for(host: str) -> HostPolicy:
    return _POLICIES.get(host.lower(), _DEFAULT_POLICY)


class RateLimiter:
    """Tracks the next allowed request time for one host."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._next_at = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            self._next_at = max(now, self._next_at) + self.min_interval
        if delay > 0:
            time.sleep(delay)


_limiters: dict[str, RateLimiter] = {}
_limiters_lock = threading.Lock()


def rate_limiter(host: str, min_interval: float) -> RateLimiter:
    """Return the shared limiter for ``host``, tightening it if asked to."""

    with _limiters_lock:
        limiter = _limiters.get(host)
        if limiter is None:
            limiter = RateLimiter(min_interval)
            _limiters[host] = limiter
        elif min_interval > limiter.min_interval:
            limiter.min_interval = min_interval
        return limiter


class HttpClient:
    def __init__(
        self,
        timeout: float = 20.0,
        backoff_base: float = 0.8,
        session: requests.Session | None = None,
    ) -> None:
        self._timeout = timeout
        self._backoff_base = backoff_base
        self._session = session if session is not None else requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        host: str | None = None,
        retry_statuses: Iterable[int] = (),
    ) -> Any:
        return self._execute(
            "GET",
            url,
            params=params,
            json_body=None,
            host=host,
            retry_statuses=retry_statuses,
        )

    def post_json(
        self,
        url: str,
        payload: Any,
        *,
        host: str | None = None,
        retry_statuses: Iterable[int] = (),
    ) -> Any:
        return self._execute(
            "POST",
            url,
            params=None,
            json_body=payload,
            host=host,
            retry_statuses=retry_statuses,
        )

    def _execute(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        json_body: Any,
        host: str | None,
        retry_statuses: Iterable[int] = (),
    ) -> Any:
        resolved_host = host or urlparse(url).netloc
        policy = policy_for(resolved_host)
        limiter = rate_limiter(resolved_host, policy.min_interval)
        retryable = {408, 425, 429, 500, 502, 503, 504} | set(retry_statuses)
        last_error = "no attempt made"
        last_status: int | None = None

        for attempt in range(1, policy.max_attempts + 1):
            limiter.wait()
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                last_error = scrub(f"{type(exc).__name__}: {exc}")
                LOG.debug("%s attempt %s/%s failed: %s", resolved_host, attempt, policy.max_attempts, last_error)
                self._sleep(attempt, None)
                continue

            if response.status_code in retryable:
                last_status = response.status_code
                last_error = f"HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After")
                if _retry_after_is_long(retry_after):
                    raise ProviderError(
                        f"{resolved_host} unavailable for {retry_after}s "
                        f"(HTTP {response.status_code})",
                        host=resolved_host,
                        status=response.status_code,
                    )
                LOG.debug("%s attempt %s/%s returned %s", resolved_host, attempt, policy.max_attempts, last_error)
                self._sleep(attempt, retry_after)
                continue

            if response.status_code >= 400:
                raise ProviderError(
                    f"HTTP {response.status_code} from {resolved_host}",
                    host=resolved_host,
                    status=response.status_code,
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderError(
                    f"non-JSON response from {resolved_host}",
                    host=resolved_host,
                    status=response.status_code,
                ) from exc

            detail = _rate_limit_detail(payload)
            if detail is not None:
                last_status = 429
                last_error = f"rate limited: {detail}"
                LOG.debug(
                    "%s attempt %s/%s returned HTTP 200 with %s",
                    resolved_host,
                    attempt,
                    policy.max_attempts,
                    last_error,
                )
                self._sleep(attempt, response.headers.get("Retry-After"))
                continue

            return payload

        raise ProviderError(
            f"{resolved_host} unavailable after {policy.max_attempts} attempts ({last_error})",
            host=resolved_host,
            status=last_status,
        )

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        delay = self._backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(min(delay, 20.0))
