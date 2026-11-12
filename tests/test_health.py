"""Connection checks: status classification and secret-free reporting."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from wallet_analyzer.config import Settings
from wallet_analyzer.health import (
    FAILED,
    NEEDS_CONFIG,
    OK,
    exit_code,
    format_summary,
    format_table,
    run_checks,
)
from wallet_analyzer.models import Network
from wallet_analyzer.net import ProviderError
from wallet_analyzer.prices import PriceError, PriceSnapshot
from wallet_analyzer.providers.base import ProviderResult

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def make_settings(**overrides) -> Settings:
    base: dict[str, object] = dict(
        etherscan_api_key=None,
        bscscan_api_key=None,
        blockchain_api_key=None,
        solana_rpc_url="https://api.mainnet-beta.solana.com",
        bsc_rpc_url="https://bsc-dataseed.binance.org",
        min_usd=Decimal("50000"),
        max_usd=Decimal("900000"),
        activity_days=30,
        max_workers=4,
        request_timeout=20.0,
        price_ttl_seconds=300,
        allow_keyless=True,
        output_dir=Path("out"),
        cache_dir=Path("cache"),
        log_level="INFO",
    )
    base.update(overrides)
    return Settings(**base)


class StubProvider:
    def __init__(self, network: Network, result: ProviderResult | None = None, error=None):
        self.network = network
        self.result = result
        self.error = error

    def fetch(self, address: str) -> ProviderResult:
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class StubPrices:
    def __init__(self, snapshot: PriceSnapshot | None = None, error=None):
        self.snapshot = snapshot
        self.error = error

    def get(self, *, force_refresh: bool = False) -> PriceSnapshot:
        if self.error is not None:
            raise self.error
        assert self.snapshot is not None
        return self.snapshot


def result(balance: str = "1", *, activity: bool = True, notes=None, source="stub") -> ProviderResult:
    return ProviderResult(
        balance=Decimal(balance),
        balance_verified=True,
        last_tx_at=NOW,
        activity_verified=activity,
        balance_source=source,
        activity_source=source if activity else "",
        notes=list(notes or []),
    )


def snapshot() -> PriceSnapshot:
    return PriceSnapshot(
        prices={
            "BTC": Decimal("74608"),
            "ETH": Decimal("2078.26"),
            "BNB": Decimal("686.12"),
            "SOL": Decimal("84.83"),
        },
        source="CoinGecko simple/price",
        fetched_at=NOW,
    )


def all_good(bsc_activity: bool = True, bsc_notes=None) -> dict:
    return {
        Network.BITCOIN: StubProvider(Network.BITCOIN, result("0.25")),
        Network.ETHEREUM: StubProvider(Network.ETHEREUM, result("5")),
        Network.BSC: StubProvider(
            Network.BSC, result("2", activity=bsc_activity, notes=bsc_notes)
        ),
        Network.SOLANA: StubProvider(Network.SOLANA, result("100")),
    }


def run(settings: Settings, providers: dict, prices: StubPrices | None = None):
    return run_checks(settings, providers=providers, price_service=prices or StubPrices(snapshot()))


def find(checks, needle: str):
    return next(check for check in checks if needle in check.service)


def test_everything_ok() -> None:
    checks = run(make_settings(), all_good())

    assert [check.status for check in checks] == [OK] * 6
    assert exit_code(checks) == 0
    assert "6 OK" in format_summary(checks)


def test_bsc_activity_without_a_key_needs_configuration_not_failure() -> None:
    checks = run(
        make_settings(),
        all_good(
            bsc_activity=False,
            bsc_notes=["BNB Smart Chain activity requires an Etherscan V2 key"],
        ),
    )

    activity = find(checks, "activity")
    assert activity.status == NEEDS_CONFIG
    assert activity.required is False
    assert "BSCSCAN_API_KEY" in activity.action
    assert exit_code(checks) == 0  # balance-only BSC is a supported mode


def test_bsc_activity_failure_with_a_key_is_reported() -> None:
    checks = run(
        make_settings(bscscan_api_key="BSCSCAN-FAKE-KEY-0002"),
        all_good(
            bsc_activity=False,
            bsc_notes=[
                "activity lookup failed: Etherscan V2 (BNB Smart Chain): "
                "Free API access is not supported for this chain."
            ],
        ),
    )

    activity = find(checks, "activity")
    assert activity.status == FAILED
    assert "Free API access is not supported" in activity.detail


def test_required_failure_breaks_the_exit_code() -> None:
    providers = all_good()
    providers[Network.BITCOIN] = StubProvider(
        Network.BITCOIN,
        error=ProviderError("https://rpc.example.com/v2/TOKENVALUE?api-key=ALSO-SECRET"),
    )
    checks = run(make_settings(), providers)

    bitcoin = find(checks, "Bitcoin")
    assert bitcoin.status == FAILED
    assert exit_code(checks) == 1

    table = format_table(checks)
    assert "TOKENVALUE" not in table
    assert "ALSO-SECRET" not in table
    assert "rpc.example.com" in table


def test_price_feed_failure_is_reported() -> None:
    checks = run(
        make_settings(),
        all_good(),
        StubPrices(error=PriceError("no price source reachable: coingecko: down")),
    )

    prices = find(checks, "Prices")
    assert prices.status == FAILED
    assert exit_code(checks) == 1


def test_table_lists_every_network() -> None:
    table = format_table(run(make_settings(), all_good()))

    for expected in ("Bitcoin", "Ethereum", "BNB Smart Chain balance", "Solana", "Prices"):
        assert expected in table
