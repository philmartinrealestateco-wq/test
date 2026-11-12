"""Orchestration: dedupe, validate, fetch, value, classify, filter."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from .bep20 import Bep20Provider
from .models import (
    ActivityStatus,
    AddressEntry,
    AnalysisResult,
    AnalysisSettings,
    Network,
    VerificationStatus,
    WalletRecord,
)
from .net import ProviderError
from .prices import PriceSnapshot, PriceService
from .providers.base import Provider
from .validators import validate

LOG = logging.getLogger(__name__)

ProgressCallback = Callable[[WalletRecord, int, int], None]


class WalletAnalyzer:
    def __init__(
        self,
        providers: dict[Network, Provider],
        price_service: PriceService,
        settings: AnalysisSettings,
        *,
        max_workers: int = 4,
        bep20: Bep20Provider | None = None,
    ) -> None:
        self._providers = providers
        self._prices = price_service
        self._settings = settings
        self._max_workers = max_workers
        self._bep20 = bep20

    def analyze(
        self,
        entries: list[AddressEntry],
        *,
        prices: PriceSnapshot | None = None,
        progress: ProgressCallback | None = None,
        now: datetime | None = None,
    ) -> AnalysisResult:
        started = datetime.now(timezone.utc)
        as_of = now or started

        unique, duplicate_count = _dedupe(entries)
        if not unique:
            raise ValueError("no addresses to analyse")

        snapshot = prices or self._prices.get()

        records: list[WalletRecord] = []
        pending: list[AddressEntry] = []

        for entry in unique:
            outcome = validate(entry.network, entry.address)
            if not outcome.valid:
                records.append(
                    WalletRecord(
                        address=entry.address,
                        network=entry.network,
                        label=entry.label,
                        verification_status=VerificationStatus.INVALID,
                        activity_status=ActivityStatus.UNKNOWN,
                        data_source="none (address rejected before lookup)",
                        notes=[outcome.reason],
                    )
                )
                continue
            pending.append(
                AddressEntry(network=entry.network, address=outcome.normalized, label=entry.label)
            )

        completed = 0
        total = len(pending)

        if pending:
            with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                futures = {
                    pool.submit(self._inspect, entry, snapshot, as_of): entry
                    for entry in pending
                }
                for future in as_completed(futures):
                    entry = futures[future]
                    try:
                        record = future.result()
                    except Exception as exc:  # noqa: BLE001 - one bad wallet must not end the run
                        LOG.exception("unexpected failure inspecting %s", entry.address)
                        record = WalletRecord(
                            address=entry.address,
                            network=entry.network,
                            label=entry.label,
                            verification_status=VerificationStatus.UNVERIFIED,
                            activity_status=ActivityStatus.UNKNOWN,
                            data_source="none",
                            notes=[f"internal error: {type(exc).__name__}"],
                        )
                    records.append(record)
                    completed += 1
                    if progress:
                        progress(record, completed, total)

        # Token prices are fetched once for the contracts actually held, then the
        # verification verdict is re-derived from complete data only.
        self._price_tokens(records)
        for record in records:
            if record.verification_status is not VerificationStatus.INVALID:
                record.verification_status = record.classify()

        if duplicate_count:
            LOG.info("collapsed %s duplicate address(es)", duplicate_count)

        matches = [
            record
            for record in records
            if record.verification_status is VerificationStatus.VERIFIED
            and record.activity_status is ActivityStatus.ACTIVE
            and record.in_balance_range(self._settings.min_usd, self._settings.max_usd)
        ]

        _sort_by_usd(records)
        _sort_by_usd(matches)

        return AnalysisResult(
            records=records,
            matches=matches,
            settings=self._settings,
            prices=snapshot.prices,
            price_source=snapshot.source,
            price_fetched_at=snapshot.fetched_at,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )

    # -- internals -------------------------------------------------------

    def _inspect(
        self, entry: AddressEntry, prices: PriceSnapshot, as_of: datetime
    ) -> WalletRecord:
        record = WalletRecord(
            address=entry.address,
            network=entry.network,
            label=entry.label,
        )
        price = prices.usd(entry.network.native_symbol)
        record.price_usd = price

        provider = self._providers.get(entry.network)
        if provider is None:
            record.notes.append(f"no provider configured for {entry.network.label}")
            return record

        try:
            outcome = provider.fetch(entry.address)
        except ProviderError as exc:
            record.data_source = "lookup failed"
            record.notes.append(f"lookup failed: {exc}")
            return record

        record.balance = outcome.balance
        record.balance_verified = outcome.balance_verified
        record.activity_verified = outcome.activity_verified
        record.data_source = outcome.sources or "unknown"
        record.notes.extend(outcome.notes)

        if outcome.balance is not None and price is not None:
            record.usd_value = outcome.balance * price
        elif price is None:
            record.notes.append(f"no USD price available for {record.native_symbol}")

        if self._bep20 is not None and entry.network is Network.BSC:
            self._attach_tokens(record, entry.address)
        elif entry.network is Network.BSC:
            record.notes.append("BEP-20 token balances were not checked (token scanning disabled)")

        record.last_tx_at = outcome.last_tx_at
        if outcome.last_tx_at is not None:
            record.days_since_last_tx = max(0, (as_of - outcome.last_tx_at).days)

        if outcome.activity_verified:
            window = timedelta(days=self._settings.activity_days)
            if outcome.last_tx_at is None:
                record.activity_status = ActivityStatus.INACTIVE
                record.notes.append("no confirmed transactions found")
            elif as_of - outcome.last_tx_at <= window:
                record.activity_status = ActivityStatus.ACTIVE
            else:
                record.activity_status = ActivityStatus.INACTIVE
        else:
            record.activity_status = ActivityStatus.UNKNOWN

        record.verification_status = record.classify()
        return record

    def _attach_tokens(self, record: WalletRecord, address: str) -> None:
        """Check the configured BEP-20 allowlist; valuation happens once, later."""

        record.tokens_checked = True
        try:
            scan = self._bep20.scan(address) if self._bep20 else None
        except ProviderError as exc:
            record.token_scan_complete = False
            record.notes.append(f"BEP-20 lookup failed: {exc}")
            return
        if scan is None:  # pragma: no cover - guarded by the caller
            return

        record.token_scan_complete = scan.complete
        record.token_holdings = sorted(
            (holding for holding in scan.holdings if holding.raw_balance > 0),
            key=lambda holding: holding.amount,
            reverse=True,
        )
        record.token_note = (
            f"{scan.checked} token contract(s) checked; unlisted tokens are not detected"
        )
        if scan.note:
            record.notes.append(scan.note)

    def _price_tokens(self, records: list[WalletRecord]) -> None:
        """Price only the contracts actually held, then total each wallet."""

        checked = [record for record in records if record.tokens_checked]
        if not checked:
            return

        contracts = sorted(
            {holding.contract.lower() for record in checked for holding in record.token_holdings}
        )
        prices: dict[str, Decimal] = {}
        if contracts:
            try:
                prices = self._prices.get_token_prices(contracts).prices
            except Exception as exc:  # noqa: BLE001 - pricing must never end the run
                LOG.warning("BEP-20 valuation failed; continuing without token values: %s", exc)
                prices = {}

        for record in checked:
            unpriced = 0
            for holding in record.token_holdings:
                price = prices.get(holding.contract.lower())
                if price is None:
                    holding.note = "no USD price"
                    unpriced += 1
                else:
                    holding.price_usd = price
                    holding.usd_value = holding.amount * price

            record.token_holdings.sort(
                key=lambda holding: holding.usd_value or Decimal(0), reverse=True
            )

            if record.token_scan_complete and not unpriced:
                record.tokens_usd = sum(
                    (holding.usd_value or Decimal(0) for holding in record.token_holdings),
                    Decimal(0),
                )
            else:
                record.tokens_usd = None
                if unpriced:
                    record.notes.append(
                        f"{unpriced} token holding(s) have no USD price; valuation incomplete"
                    )


def _dedupe(entries: list[AddressEntry]) -> tuple[list[AddressEntry], int]:
    seen: dict[tuple[Network, str], AddressEntry] = {}
    for entry in entries:
        seen.setdefault(entry.dedupe_key, entry)
    return list(seen.values()), len(entries) - len(seen)


def _sort_by_usd(records: list[WalletRecord]) -> None:
    records.sort(
        key=lambda record: (
            record.total_usd is not None,
            record.total_usd if record.total_usd is not None else Decimal(0),
        ),
        reverse=True,
    )
