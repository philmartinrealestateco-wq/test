"""Analyzer behaviour: filtering, verification and failure handling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from wallet_analyzer.analyzer import WalletAnalyzer
from wallet_analyzer.bep20 import TokenScan
from wallet_analyzer.models import (
    ActivityStatus,
    AddressEntry,
    AnalysisSettings,
    Network,
    TokenHolding,
    VerificationStatus,
)
from wallet_analyzer.net import ProviderError
from wallet_analyzer.prices import PriceSnapshot, TokenPriceSnapshot
from wallet_analyzer.providers.base import ProviderResult

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
ETH_ADDRESS = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
SOL_ADDRESS = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
TOKEN_USDT = "0x55d398326f99059fF775485246999027B3197955"
SETTINGS = AnalysisSettings(min_usd=Decimal("50000"), max_usd=Decimal("900000"), activity_days=30)


class FakePriceService:
    def __init__(self, token_prices: dict[str, Decimal] | None = None) -> None:
        self.snapshot = PriceSnapshot(
            prices={
                "BTC": Decimal("74608"),
                "ETH": Decimal("2078.26"),
                "BNB": Decimal("686.12"),
                "SOL": Decimal("84.83"),
            },
            source="test feed",
            fetched_at=NOW,
        )
        self.token_prices = {k.lower(): v for k, v in (token_prices or {}).items()}

    def get(self, *, force_refresh: bool = False) -> PriceSnapshot:
        return self.snapshot

    def get_token_prices(
        self,
        contracts,
        *,
        platform: str = "binance-smart-chain",
        force_refresh: bool = False,
    ) -> TokenPriceSnapshot:
        wanted = [contract.lower() for contract in contracts]
        missing = {contract for contract in wanted if contract not in self.token_prices}
        return TokenPriceSnapshot(dict(self.token_prices), "test token feed", NOW, missing)


class FakeBep20:
    def __init__(self, holdings=None, *, complete: bool = True, error: Exception | None = None):
        self._holdings = list(holdings or [])
        self._complete = complete
        self._error = error
        self._contracts = [holding.contract for holding in self._holdings] or [TOKEN_USDT]

    @property
    def contracts(self) -> list[str]:
        return list(self._contracts)

    def scan(self, address: str) -> TokenScan:
        if self._error is not None:
            raise self._error
        fresh = [
            TokenHolding(
                contract=holding.contract,
                symbol=holding.symbol,
                decimals=holding.decimals,
                raw_balance=holding.raw_balance,
                amount=holding.amount,
            )
            for holding in self._holdings
        ]
        return TokenScan(
            holdings=fresh,
            checked=len(self._contracts),
            failed=[] if self._complete else ["0xdead"],
            complete=self._complete,
            note="" if self._complete else "1 of 1 token contracts could not be read",
        )


class FakeProvider:
    def __init__(self, network=Network.ETHEREUM, result=None, error=None) -> None:
        self.network = network
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def fetch(self, address: str) -> ProviderResult:
        self.calls.append(address)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def verified_result(balance: str, days_ago: int | None) -> ProviderResult:
    return ProviderResult(
        balance=Decimal(balance),
        balance_verified=True,
        last_tx_at=None if days_ago is None else NOW - timedelta(days=days_ago),
        activity_verified=True,
        balance_source="fake",
        activity_source="fake",
    )


def build(provider: FakeProvider) -> WalletAnalyzer:
    return WalletAnalyzer(
        {provider.network: provider}, FakePriceService(), SETTINGS, max_workers=2
    )


def analyse(provider: FakeProvider, entries: list[AddressEntry]):
    return build(provider).analyze(entries, now=NOW)


def test_active_wallet_inside_range_is_matched() -> None:
    provider = FakeProvider(result=verified_result("30", days_ago=2))
    result = analyse(provider, [AddressEntry(Network.ETHEREUM, ETH_ADDRESS)])

    assert len(result.records) == 1
    record = result.records[0]
    assert record.usd_value == Decimal("30") * Decimal("2078.26")
    assert record.activity_status is ActivityStatus.ACTIVE
    assert record.verification_status is VerificationStatus.VERIFIED
    assert result.matches == [record]
    assert record.days_since_last_tx == 2


def test_stale_wallet_is_inactive_and_not_matched() -> None:
    provider = FakeProvider(result=verified_result("30", days_ago=45))
    result = analyse(provider, [AddressEntry(Network.ETHEREUM, ETH_ADDRESS)])

    assert result.records[0].activity_status is ActivityStatus.INACTIVE
    assert result.matches == []
    assert result.in_range_count == 1


def test_unverified_activity_is_partial_and_never_matched() -> None:
    provider = FakeProvider(
        network=Network.BSC,
        result=ProviderResult(
            balance=Decimal("100"),
            balance_verified=True,
            activity_verified=False,
            balance_source="bsc rpc",
            notes=["activity requires a paid plan"],
        ),
    )
    result = analyse(provider, [AddressEntry(Network.BSC, ETH_ADDRESS)])

    record = result.records[0]
    assert record.verification_status is VerificationStatus.PARTIAL
    assert record.activity_status is ActivityStatus.UNKNOWN
    assert record.usd_value == Decimal("100") * Decimal("686.12")
    assert result.matches == []


def test_invalid_address_never_reaches_the_provider() -> None:
    provider = FakeProvider(result=verified_result("1", days_ago=1))
    result = analyse(provider, [AddressEntry(Network.ETHEREUM, "0x1234")])

    assert provider.calls == []
    assert result.records[0].verification_status is VerificationStatus.INVALID
    assert "0x followed by 40" in result.records[0].joined_notes()


def test_provider_failure_is_unverified_not_zero() -> None:
    provider = FakeProvider(error=ProviderError("blockchain.info unavailable after 4 attempts"))
    result = analyse(provider, [AddressEntry(Network.ETHEREUM, ETH_ADDRESS)])

    record = result.records[0]
    assert record.verification_status is VerificationStatus.UNVERIFIED
    assert record.balance is None
    assert record.usd_value is None
    assert "lookup failed" in record.joined_notes()


def test_duplicate_addresses_are_collapsed_case_insensitively() -> None:
    provider = FakeProvider(result=verified_result("30", days_ago=1))
    result = analyse(
        provider,
        [
            AddressEntry(Network.ETHEREUM, ETH_ADDRESS),
            AddressEntry(Network.ETHEREUM, ETH_ADDRESS.lower()),
        ],
    )

    assert len(provider.calls) == 1
    assert result.total_addresses == 1


def test_same_address_on_two_networks_is_two_wallets() -> None:
    eth = FakeProvider(Network.ETHEREUM, result=verified_result("30", days_ago=1))
    bsc = FakeProvider(Network.BSC, result=verified_result("100", days_ago=1))
    analyzer = WalletAnalyzer(
        {Network.ETHEREUM: eth, Network.BSC: bsc}, FakePriceService(), SETTINGS
    )
    result = analyzer.analyze(
        [
            AddressEntry(Network.ETHEREUM, ETH_ADDRESS),
            AddressEntry(Network.BSC, ETH_ADDRESS),
        ],
        now=NOW,
    )

    assert result.total_addresses == 2
    assert len(result.matches) == 2


def test_out_of_range_balances_are_counted_but_not_matched() -> None:
    provider = FakeProvider(result=verified_result("1000", days_ago=1))
    result = analyse(provider, [AddressEntry(Network.ETHEREUM, ETH_ADDRESS)])

    assert result.records[0].usd_value == Decimal("2078260.00")
    assert result.in_range_count == 0
    assert result.matches == []


def test_records_are_sorted_by_usd_descending() -> None:
    eth = FakeProvider(Network.ETHEREUM, result=verified_result("10", days_ago=1))
    bsc = FakeProvider(Network.BSC, result=verified_result("200", days_ago=1))
    btc = FakeProvider(Network.BITCOIN, result=verified_result("1", days_ago=1))
    analyzer = WalletAnalyzer(
        {Network.ETHEREUM: eth, Network.BSC: bsc, Network.BITCOIN: btc},
        FakePriceService(),
        SETTINGS,
    )
    result = analyzer.analyze(
        [
            AddressEntry(Network.ETHEREUM, ETH_ADDRESS),
            AddressEntry(Network.BSC, ETH_ADDRESS),
            AddressEntry(Network.BITCOIN, "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"),
        ],
        now=NOW,
    )

    values = [record.usd_value for record in result.records]
    assert values == sorted(values, reverse=True)


def test_empty_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        analyze_empty()


def analyze_empty() -> None:
    WalletAnalyzer(
        {Network.ETHEREUM: FakeProvider()}, FakePriceService(), SETTINGS
    ).analyze([])


# -- BEP-20 token valuation ---------------------------------------------


def usdt_holding(amount: str = "500") -> TokenHolding:
    raw = int(Decimal(amount) * (Decimal(10) ** 18))
    return TokenHolding(
        contract=TOKEN_USDT,
        symbol="USDT",
        decimals=18,
        raw_balance=raw,
        amount=Decimal(amount),
    )


def analyse_bsc(bep20, token_prices=None, *, settings=SETTINGS, days_ago=1):
    provider = FakeProvider(Network.BSC, result=verified_result("100", days_ago))
    analyzer = WalletAnalyzer(
        {Network.BSC: provider},
        FakePriceService(token_prices),
        settings,
        bep20=bep20,
    )
    return analyzer.analyze([AddressEntry(Network.BSC, ETH_ADDRESS)], now=NOW)


def test_native_and_token_values_are_reported_separately() -> None:
    result = analyse_bsc(
        FakeBep20([usdt_holding("500")]), {TOKEN_USDT: Decimal("1")}
    )
    record = result.records[0]

    assert record.usd_value == Decimal("100") * Decimal("686.12")  # native only
    assert record.tokens_usd == Decimal("500")
    assert record.total_usd == Decimal("69112.00")
    assert record.token_count == 1
    assert record.tokens_checked is True


def test_combined_total_drives_the_range_filter() -> None:
    settings = AnalysisSettings(
        min_usd=Decimal("68700"), max_usd=Decimal("70000"), activity_days=30
    )
    result = analyse_bsc(
        FakeBep20([usdt_holding("500")]), {TOKEN_USDT: Decimal("1")}, settings=settings
    )
    record = result.records[0]

    assert record.usd_value < settings.min_usd  # native alone is out of range
    assert record.in_balance_range(settings.min_usd, settings.max_usd)
    assert result.in_range_count == 1


def test_zero_balances_do_not_count_as_holdings() -> None:
    result = analyse_bsc(FakeBep20([usdt_holding("0")]), {TOKEN_USDT: Decimal("1")})
    record = result.records[0]

    assert record.token_count == 0
    assert record.tokens_usd == Decimal("0")
    assert record.total_usd == record.usd_value


def test_unpriced_token_keeps_the_record_partial() -> None:
    result = analyse_bsc(FakeBep20([usdt_holding("500")]), {})
    record = result.records[0]

    assert record.tokens_usd is None
    assert record.total_usd is None
    assert record.verification_status is VerificationStatus.PARTIAL
    assert "no USD price" in record.joined_notes()


def test_verified_is_never_granted_with_incomplete_token_coverage() -> None:
    result = analyse_bsc(FakeBep20([usdt_holding("500")]), {})
    record = result.records[0]

    # native balance and activity are both verified in this fixture, so only the
    # token gap can be holding it back from VERIFIED.
    assert record.activity_status is ActivityStatus.ACTIVE
    assert record.verification_status is not VerificationStatus.VERIFIED
    assert record.verification_status is VerificationStatus.PARTIAL


def test_unreadable_token_contract_keeps_the_record_partial() -> None:
    result = analyse_bsc(
        FakeBep20([usdt_holding("500")], complete=False), {TOKEN_USDT: Decimal("1")}
    )
    record = result.records[0]

    assert record.tokens_usd is None
    assert record.total_usd is None
    assert record.verification_status is VerificationStatus.PARTIAL
    assert "could not be read" in record.joined_notes()


def test_token_lookup_failure_degrades_to_partial() -> None:
    result = analyse_bsc(
        FakeBep20(error=ProviderError("bsc node down")), {TOKEN_USDT: Decimal("1")}
    )
    record = result.records[0]

    assert record.tokens_checked is True
    assert record.verification_status is VerificationStatus.PARTIAL
    assert "token lookup failed" in record.joined_notes()


def test_ethereum_records_ignore_the_token_layer() -> None:
    provider = FakeProvider(Network.ETHEREUM, result=verified_result("30", days_ago=1))
    analyzer = WalletAnalyzer(
        {Network.ETHEREUM: provider},
        FakePriceService(),
        SETTINGS,
        bep20=FakeBep20([usdt_holding("500")]),
    )
    result = analyzer.analyze([AddressEntry(Network.ETHEREUM, ETH_ADDRESS)], now=NOW)
    record = result.records[0]

    assert record.tokens_checked is False
    assert record.total_usd == record.usd_value
    assert record.verification_status is VerificationStatus.VERIFIED


def test_ethereum_erc20_holdings_are_valued() -> None:
    provider = FakeProvider(Network.ETHEREUM, result=verified_result("1", days_ago=1))
    analyzer = WalletAnalyzer(
        {Network.ETHEREUM: provider},
        FakePriceService({TOKEN_USDT: Decimal("1")}),
        SETTINGS,
        erc20=FakeBep20([usdt_holding("500")]),
    )
    result = analyzer.analyze([AddressEntry(Network.ETHEREUM, ETH_ADDRESS)], now=NOW)
    record = result.records[0]

    assert record.tokens_checked is True
    assert record.tokens_usd == Decimal("500")
    assert record.total_usd == record.usd_value + Decimal("500")


def test_solana_rpc_failure_is_unverified_not_zero() -> None:
    provider = FakeProvider(
        Network.SOLANA,
        error=ProviderError("HTTP 429 from api.mainnet-beta.solana.com"),
    )
    result = analyse(provider, [AddressEntry(Network.SOLANA, SOL_ADDRESS)])
    record = result.records[0]

    assert record.verification_status is VerificationStatus.UNVERIFIED
    assert record.balance is None
    assert record.usd_value is None
    assert "lookup failed" in record.joined_notes()


def test_pricing_engine_failure_does_not_abort_the_run() -> None:
    class ExplodingPrices(FakePriceService):
        def get_token_prices(
            self,
            contracts,
            *,
            platform: str = "binance-smart-chain",
            force_refresh=False,
        ):
            raise RuntimeError("pricing engine exploded")

    provider = FakeProvider(Network.BSC, result=verified_result("100", days_ago=1))
    analyzer = WalletAnalyzer(
        {Network.BSC: provider},
        ExplodingPrices(),
        SETTINGS,
        bep20=FakeBep20([usdt_holding("500")]),
    )
    result = analyzer.analyze([AddressEntry(Network.BSC, ETH_ADDRESS)], now=NOW)
    record = result.records[0]

    assert result.total_addresses == 1
    assert record.tokens_usd is None
    assert record.verification_status is VerificationStatus.PARTIAL
