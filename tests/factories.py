"""Synthetic objects shared by the test modules (no network, no clock drift)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from wallet_analyzer.models import (
    ActivityStatus,
    AnalysisResult,
    AnalysisSettings,
    Network,
    VerificationStatus,
    WalletRecord,
)

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)

PRICES = {
    "BTC": Decimal("74608"),
    "ETH": Decimal("2078.26"),
    "BNB": Decimal("686.12"),
    "SOL": Decimal("84.83"),
}


def make_record(
    *,
    address: str = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    network: Network = Network.ETHEREUM,
    balance: Decimal | None = Decimal("1"),
    usd_value: Decimal | None = Decimal("2078.26"),
    days_ago: int | None = 3,
    activity: ActivityStatus = ActivityStatus.ACTIVE,
    verification: VerificationStatus = VerificationStatus.VERIFIED,
    source: str = "unit-test source",
    notes: list[str] | None = None,
) -> WalletRecord:
    return WalletRecord(
        address=address,
        network=network,
        balance=balance,
        price_usd=PRICES.get(network.native_symbol),
        usd_value=usd_value,
        last_tx_at=None if days_ago is None else NOW - timedelta(days=days_ago),
        days_since_last_tx=days_ago,
        activity_status=activity,
        verification_status=verification,
        data_source=source,
        notes=list(notes or []),
    )


def make_result(
    records: list[WalletRecord],
    *,
    matches: list[WalletRecord] | None = None,
    settings: AnalysisSettings | None = None,
) -> AnalysisResult:
    resolved = settings or AnalysisSettings(
        min_usd=Decimal("50000"), max_usd=Decimal("900000"), activity_days=30
    )
    if matches is None:
        matches = [
            record
            for record in records
            if record.verification_status is VerificationStatus.VERIFIED
            and record.activity_status is ActivityStatus.ACTIVE
            and record.in_balance_range(resolved.min_usd, resolved.max_usd)
        ]
    return AnalysisResult(
        records=records,
        matches=matches,
        settings=resolved,
        prices=dict(PRICES),
        price_source="CoinGecko simple/price",
        price_fetched_at=NOW,
        started_at=NOW - timedelta(seconds=2),
        finished_at=NOW,
    )


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code: int, payload=None, headers=None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeSession:
    """Serves a scripted list of responses and counts attempts."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.attempts = 0
        self.headers: dict[str, str] = {}

    def request(self, method, url, **_kwargs):
        self.attempts += 1
        if not self._responses:
            raise AssertionError("unexpected extra request")
        return self._responses.pop(0)

    def close(self) -> None:
        return None
