"""Price feed: primary source, fallback, caching, and refusal to guess."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tests.factories import FakeResponse, FakeSession
from wallet_analyzer.net import HttpClient, ProviderError
from wallet_analyzer.prices import PriceError, PriceService


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[str] = []
        self.last_retry_statuses: tuple[int, ...] = ()

    def get_json(self, url, params=None, host=None, retry_statuses=()):
        self.calls.append(host or url)
        self.last_retry_statuses = tuple(retry_statuses)
        return self.handler(url, params)


COINGECKO_PAYLOAD = {
    "bitcoin": {"usd": 74608, "last_updated_at": 1794432087},
    "ethereum": {"usd": 2078.26, "last_updated_at": 1794432057},
    "binancecoin": {"usd": 686.12, "last_updated_at": 1794432057},
    "solana": {"usd": 84.83, "last_updated_at": 1794432078},
}


def test_coingecko_prices_are_parsed_as_decimals() -> None:
    client = FakeClient(lambda url, params: COINGECKO_PAYLOAD)
    snapshot = PriceService(client).get()

    assert snapshot.prices["ETH"] == Decimal("2078.26")
    assert snapshot.source.startswith("CoinGecko")
    assert snapshot.updated_at == datetime.fromtimestamp(1794432087, tz=timezone.utc)


def test_coingecko_payload_missing_asset_falls_back() -> None:
    def handler(url, params):
        if "coingecko" in url:
            return {"bitcoin": {"usd": 74608}}
        return {"data": {"amount": "100.00"}}

    snapshot = PriceService(FakeClient(handler)).get()
    assert snapshot.source == "Coinbase spot prices"
    assert snapshot.prices["SOL"] == Decimal("100.00")


def test_all_sources_failing_raises_instead_of_guessing() -> None:
    def handler(url, params):
        raise ProviderError("upstream down")

    with pytest.raises(PriceError, match="no price source reachable"):
        PriceService(FakeClient(handler)).get()


def test_cache_is_used_within_ttl(tmp_path: Path) -> None:
    cache = tmp_path / "prices.json"
    client = FakeClient(lambda url, params: COINGECKO_PAYLOAD)
    service = PriceService(client, ttl_seconds=600, cache_path=cache)

    first = service.get()
    calls_after_first = len(client.calls)
    second = service.get()

    assert len(client.calls) == calls_after_first
    assert second.prices == first.prices


def test_expired_cache_triggers_a_refresh(tmp_path: Path) -> None:
    cache = tmp_path / "prices.json"
    old = datetime.now(timezone.utc) - timedelta(hours=4)
    cache.write_text(
        '{"fetched_at": "%s", "source": "old", "prices": {"BTC": "1", "ETH": "1", "BNB": "1", "SOL": "1"}}'
        % old.isoformat(),
        encoding="utf-8",
    )

    client = FakeClient(lambda url, params: COINGECKO_PAYLOAD)
    snapshot = PriceService(client, ttl_seconds=60, cache_path=cache).get()

    assert snapshot.source.startswith("CoinGecko")
    assert snapshot.prices["BTC"] == Decimal("74608")


def test_stale_cache_is_reused_when_every_source_fails(tmp_path: Path) -> None:
    cache = tmp_path / "prices.json"
    old = datetime.now(timezone.utc) - timedelta(hours=4)
    cache.write_text(
        '{"fetched_at": "%s", "source": "old feed", "prices": {"BTC": "1", "ETH": "2", "BNB": "3", "SOL": "4"}}'
        % old.isoformat(),
        encoding="utf-8",
    )

    def handler(url, params):
        raise ProviderError("offline")

    snapshot = PriceService(FakeClient(handler), cache_path=cache).get()
    assert snapshot.stale is True
    assert snapshot.prices["SOL"] == Decimal("4")


def test_non_positive_price_is_rejected() -> None:
    def handler(url, params):
        return {"bitcoin": {"usd": 0}, "ethereum": {"usd": 1}, "binancecoin": {"usd": 1}, "solana": {"usd": 1}}

    with pytest.raises(PriceError):
        PriceService(FakeClient(handler)).get()


# -- BEP-20 token prices -------------------------------------------------

USDT = "0x55d398326f99059ff775485246999027b3197955"
CAKE = "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82"


def token_handler(mapping):
    def handler(url, params):
        if "token_price" in url:
            return mapping
        return COINGECKO_PAYLOAD

    return handler


def test_token_prices_are_keyed_by_lowercased_contract() -> None:
    client = FakeClient(
        token_handler({USDT: {"usd": 1.0}, CAKE.upper().replace("0X", "0x"): {"usd": 2.5}})
    )
    snapshot = PriceService(client).get_token_prices([USDT, CAKE])

    assert snapshot.prices[USDT] == Decimal("1.0")
    assert snapshot.prices[CAKE] == Decimal("2.5")
    assert snapshot.missing == set()
    assert snapshot.source.startswith("CoinGecko")


def test_token_price_response_omits_unknown_contracts() -> None:
    client = FakeClient(token_handler({USDT: {"usd": "1.0"}}))
    snapshot = PriceService(client).get_token_prices([USDT, CAKE])

    assert snapshot.usd(USDT) == Decimal("1.0")
    assert snapshot.usd(CAKE) is None
    assert snapshot.missing == {CAKE}


def test_token_price_failure_returns_unavailable_instead_of_guessing() -> None:
    def handler(url, params):
        raise ProviderError("coingecko down")

    snapshot = PriceService(FakeClient(handler)).get_token_prices([USDT])
    assert snapshot.prices == {}
    assert snapshot.missing == {USDT}
    assert snapshot.source == "unavailable"


def test_token_price_cache_avoids_a_second_fetch(tmp_path: Path) -> None:
    cache = tmp_path / "token_prices.json"
    client = FakeClient(token_handler({USDT: {"usd": "1.0"}}))
    service = PriceService(client, ttl_seconds=600, token_cache_path=cache)

    first = service.get_token_prices([USDT])
    calls_after_first = len(client.calls)
    second = service.get_token_prices([USDT])

    assert len(client.calls) == calls_after_first
    assert second.prices == first.prices


def test_empty_token_request_is_not_sent() -> None:
    client = FakeClient(token_handler({}))
    snapshot = PriceService(client).get_token_prices([])
    assert snapshot.prices == {}
    assert client.calls == []


# -- retry and isolation -------------------------------------------------


def test_token_price_retries_through_429(monkeypatch) -> None:
    monkeypatch.setattr("wallet_analyzer.net.time.sleep", lambda seconds: None)
    session = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(200, {USDT: {"usd": "1.0"}}),
        ]
    )

    snapshot = PriceService(HttpClient(session=session)).get_token_prices([USDT])

    assert snapshot.prices[USDT] == Decimal("1.0")
    assert session.attempts == 2


def test_token_price_gives_up_after_persistent_429(monkeypatch) -> None:
    monkeypatch.setattr("wallet_analyzer.net.time.sleep", lambda seconds: None)
    session = FakeSession([FakeResponse(429, headers={}) for _ in range(4)])

    snapshot = PriceService(HttpClient(session=session)).get_token_prices([USDT])

    assert snapshot.prices == {}
    assert snapshot.missing == {USDT}
    assert snapshot.source == "unavailable"
    assert session.attempts == 4


def test_token_price_retries_through_400(monkeypatch) -> None:
    monkeypatch.setattr("wallet_analyzer.net.time.sleep", lambda seconds: None)
    session = FakeSession(
        [FakeResponse(400, {}), FakeResponse(200, {USDT: {"usd": "0.999"}})]
    )

    snapshot = PriceService(HttpClient(session=session)).get_token_prices([USDT])

    assert snapshot.prices[USDT] == Decimal("0.999")
    assert session.attempts == 2


def test_token_price_requests_opt_into_400_retries() -> None:
    client = FakeClient(token_handler({USDT: {"usd": "1"}}))
    PriceService(client).get_token_prices([USDT])

    assert 400 in client.last_retry_statuses


def test_one_failing_contract_does_not_discard_the_others() -> None:
    def handler(url, params):
        if params["contract_addresses"] == CAKE:
            raise ProviderError("HTTP 400 from api.coingecko.com", status=400)
        return {USDT: {"usd": "1.0"}}

    snapshot = PriceService(FakeClient(handler)).get_token_prices([USDT, CAKE])

    assert snapshot.usd(USDT) == Decimal("1.0")
    assert snapshot.missing == {CAKE}


def test_all_contracts_failing_still_returns_a_snapshot() -> None:
    def handler(url, params):
        raise ProviderError("HTTP 429 from api.coingecko.com", status=429)

    snapshot = PriceService(FakeClient(handler)).get_token_prices([USDT, CAKE])

    assert snapshot.prices == {}
    assert snapshot.missing == {USDT, CAKE}
    assert snapshot.source == "unavailable"
