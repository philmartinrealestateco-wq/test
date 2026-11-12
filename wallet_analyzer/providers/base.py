"""Provider contract shared by all chains."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from ..models import Network
from ..net import HttpClient, ProviderError


@dataclass
class ProviderResult:
    """What a provider could establish about one wallet.

    ``None`` means "not determined" and must never be rendered as zero.
    """

    balance: Decimal | None
    balance_verified: bool
    last_tx_at: datetime | None = None
    tx_count: int | None = None
    activity_verified: bool = False
    balance_source: str = ""
    activity_source: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def sources(self) -> str:
        parts: list[str] = []
        for value in (self.balance_source, self.activity_source):
            if value and value not in parts:
                parts.append(value)
        return " | ".join(parts)


@runtime_checkable
class Provider(Protocol):
    network: Network

    def fetch(self, address: str) -> ProviderResult: ...


class JsonRpcProvider:
    """Minimal JSON-RPC client for node endpoints (BSC, Solana)."""

    def __init__(
        self,
        client: HttpClient,
        url: str,
        host: str,
        *,
        env_hint: str = "RPC endpoint",
    ) -> None:
        self._client = client
        self._url = url
        self._host = host
        self._config_error = _endpoint_problem(url, env_hint)

    @property
    def endpoint(self) -> str:
        return "invalid-endpoint" if self._config_error else self._host

    @property
    def has_config_error(self) -> bool:
        return self._config_error is not None

    def call(self, method: str, params: list[Any]) -> Any:
        ok, value = self.call_one(method, params)
        if not ok:
            raise ProviderError(f"{self._host} RPC error: {value}", host=self._host)
        return value

    def call_one(self, method: str, params: list[Any]) -> tuple[bool, Any]:
        """Single call returning ``(ok, result_or_error_message)``."""

        if self._config_error:
            # Fail fast: retrying a value that is not a URL wastes the retry
            # budget and risks echoing the value in transport errors.
            raise ProviderError(self._config_error, host=self._host)

        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        response = self._client.post_json(self._url, payload, host=self._host)

        if not isinstance(response, dict):
            return False, "non-object JSON-RPC response"
        error = response.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else error
            return False, message
        if "result" not in response:
            return False, "response has no result"
        return True, response["result"]

    def call_many(self, calls: list[tuple[str, list[Any]]]) -> list[tuple[bool, Any]]:
        """Batch several calls into one HTTP request.

        Raises :class:`ProviderError` when the node does not support batches, so
        callers can fall back to individual calls.
        """

        if self._config_error:
            raise ProviderError(self._config_error, host=self._host)

        payload = [
            {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            for index, (method, params) in enumerate(calls, start=1)
        ]
        response = self._client.post_json(self._url, payload, host=self._host)

        if not isinstance(response, list):
            raise ProviderError(
                f"{self._host} did not return a JSON-RPC batch response", host=self._host
            )

        by_id: dict[Any, dict] = {}
        for item in response:
            if isinstance(item, dict) and "id" in item:
                by_id[item["id"]] = item

        results: list[tuple[bool, Any]] = []
        for index in range(1, len(calls) + 1):
            item = by_id.get(index)
            if not isinstance(item, dict) or "result" not in item:
                error = item.get("error") if isinstance(item, dict) else None
                message = error.get("message") if isinstance(error, dict) else error
                results.append((False, message or "missing batch result"))
                continue
            results.append((True, item["result"]))
        return results


def _endpoint_problem(url: str, env_hint: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return None
    # The value is deliberately not echoed: it is usually a bare API key.
    return (
        f"{env_hint} is not an absolute http(s) URL; "
        "set it to the provider's full endpoint, including scheme and host"
    )
