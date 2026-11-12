"""Bulk mode: one wallet per line, incremental CSV, isolated failures.

Each input line is analysed on its own so a wallet that fails cannot take the
batch down with it. Rows are appended to ``report.csv`` as the run progresses,
which means a crashed batch still leaves the completed work on disk.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

from .models import AddressEntry, WalletRecord
from .net import scrub
from .prices import PriceSnapshot

LOG = logging.getLogger(__name__)

COLUMNS = ("Wallet", "Network", "Native USD", "Token USD", "Total USD", "Status")
ERROR_STATUS = "ERROR"

ProgressCallback = Callable[[int, int, str], None]


class Analyzer(Protocol):
    def analyze(
        self,
        entries: list[AddressEntry],
        *,
        prices: PriceSnapshot | None = None,
        progress: Callable[[WalletRecord, int, int], None] | None = None,
    ): ...


@dataclass(frozen=True)
class BulkRow:
    wallet: str
    network: str
    native_usd: Decimal | None
    token_usd: Decimal | None
    total_usd: Decimal | None
    status: str

    def cells(self) -> list[str]:
        return [
            self.wallet,
            self.network,
            _money(self.native_usd),
            _money(self.token_usd),
            _money(self.total_usd),
            self.status,
        ]


def _money(value: Decimal | None) -> str:
    return "" if value is None else f"{value:.2f}"


def rows_for_records(records: Iterable[WalletRecord]) -> list[BulkRow]:
    return [
        BulkRow(
            wallet=record.address,
            network=record.network.label,
            native_usd=record.usd_value,
            token_usd=record.tokens_usd,
            total_usd=record.total_usd,
            status=record.verification_status.value,
        )
        for record in records
    ]


def error_rows(entries: Sequence[AddressEntry]) -> list[BulkRow]:
    """One row per entry so a failed wallet still appears in the report."""

    return [
        BulkRow(entry.address, entry.network.label, None, None, None, ERROR_STATUS)
        for entry in entries
    ]


def group_by_address(entries: Sequence[AddressEntry]) -> list[list[AddressEntry]]:
    """Keep the networks of one address together.

    A bare 0x address expands to Ethereum and BNB Smart Chain; analysing them as
    a group keeps progress tied to input lines and writes one row per network.
    """

    groups: dict[str, list[AddressEntry]] = {}
    for entry in entries:
        key = entry.address.lower() if entry.network.is_evm else entry.address
        groups.setdefault(key, []).append(entry)
    return list(groups.values())


def append_rows(path: Path, rows: Iterable[BulkRow]) -> int:
    """Append rows, writing the header only when the file is new or empty."""

    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0

    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if needs_header:
            writer.writerow(COLUMNS)
        count = 0
        for row in rows:
            writer.writerow(row.cells())
            count += 1
    return count


def run_bulk(
    groups: Sequence[Sequence[AddressEntry]],
    analyzer: Analyzer,
    prices: PriceSnapshot,
    report_path: Path,
    *,
    progress: ProgressCallback | None = None,
) -> list[BulkRow]:
    """Analyse each group, appending results and continuing past failures."""

    written: list[BulkRow] = []
    total = len(groups)

    for index, group in enumerate(groups, start=1):
        address = group[0].address
        if progress is not None:
            progress(index, total, address)

        try:
            result = analyzer.analyze(list(group), prices=prices)
            rows = rows_for_records(result.records)
        except Exception as exc:  # noqa: BLE001 - one wallet must not end the batch
            LOG.warning("wallet %s failed and was skipped: %s", address, scrub(str(exc)))
            rows = error_rows(group)

        append_rows(report_path, rows)
        written.extend(rows)

    return written
