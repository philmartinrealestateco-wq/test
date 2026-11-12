"""Bulk mode: file parsing, grouping, CSV appending and failure isolation."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from tests.factories import NOW, make_record
from wallet_analyzer.bulk import (
    COLUMNS,
    ERROR_STATUS,
    BulkRow,
    append_rows,
    error_rows,
    group_by_address,
    rows_for_records,
    run_bulk,
)
from wallet_analyzer.inputs import load_file
from wallet_analyzer.models import AddressEntry, Network, VerificationStatus
from wallet_analyzer.prices import PriceSnapshot

BTC = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
EVM = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
SOL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


def snapshot() -> PriceSnapshot:
    return PriceSnapshot(
        prices={"BTC": Decimal("74608"), "ETH": Decimal("2078.26")},
        source="test",
        fetched_at=NOW,
    )


class StubAnalyzer:
    """Returns one record per entry, unless the address is marked as failing."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()
        self.calls: list[str] = []
        self.prices_seen: list[object] = []

    def analyze(self, entries, *, prices=None, progress=None):
        address = entries[0].address
        self.calls.append(address)
        self.prices_seen.append(prices)
        if address in self.failing:
            raise RuntimeError("provider exploded")
        return SimpleNamespace(
            records=[
                make_record(address=entry.address, network=entry.network)
                for entry in entries
            ]
        )


# -- parsing and grouping ------------------------------------------------


def test_txt_file_is_parsed_one_address_per_line(tmp_path: Path) -> None:
    path = tmp_path / "wallets.txt"
    path.write_text(
        f"{BTC}\n\n# a comment\n{EVM}\n{SOL}\n",
        encoding="utf-8",
    )

    parsed = load_file(path)

    assert len(parsed.entries) == 4  # 0x expands to Ethereum + BNB Smart Chain
    assert [entry.address for entry in parsed.entries] == [BTC, EVM, EVM, SOL]


def test_groups_keep_the_two_evm_networks_together() -> None:
    entries = [
        AddressEntry(Network.BITCOIN, BTC),
        AddressEntry(Network.ETHEREUM, EVM),
        AddressEntry(Network.BSC, EVM),
        AddressEntry(Network.SOLANA, SOL),
    ]

    groups = group_by_address(entries)

    assert [len(group) for group in groups] == [1, 2, 1]
    assert groups[1][0].network is Network.ETHEREUM
    assert groups[1][1].network is Network.BSC


def test_grouping_preserves_input_order() -> None:
    entries = [
        AddressEntry(Network.SOLANA, SOL),
        AddressEntry(Network.BITCOIN, BTC),
    ]

    groups = group_by_address(entries)

    assert [group[0].address for group in groups] == [SOL, BTC]


# -- row shaping ---------------------------------------------------------


def test_rows_map_native_token_and_total() -> None:
    record = make_record(address=EVM, network=Network.BSC, usd_value=Decimal("2044.21"))
    record.tokens_usd = Decimal("231404.08")
    record.tokens_checked = True
    record.verification_status = VerificationStatus.PARTIAL

    row = rows_for_records([record])[0]

    assert row.cells() == [
        EVM,
        "BNB Smart Chain",
        "2044.21",
        "231404.08",
        "233448.29",
        "PARTIAL",
    ]


def test_rows_leave_token_fields_blank_when_not_checked() -> None:
    record = make_record(address=BTC, network=Network.BITCOIN, usd_value=Decimal("12.5"))

    row = rows_for_records([record])[0]

    assert row.cells()[2:] == ["12.50", "", "12.50", "VERIFIED"]


def test_error_rows_are_blank_with_error_status() -> None:
    rows = error_rows([AddressEntry(Network.ETHEREUM, EVM)])

    assert rows[0].cells() == [EVM, "Ethereum", "", "", "", ERROR_STATUS]


# -- CSV appending -------------------------------------------------------


def read_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def test_header_is_written_once_across_appends(tmp_path: Path) -> None:
    report = tmp_path / "report.csv"

    append_rows(report, [BulkRow("a", "Bitcoin", Decimal("1"), None, Decimal("1"), "VERIFIED")])
    append_rows(report, [BulkRow("b", "Bitcoin", Decimal("2"), None, Decimal("2"), "PARTIAL")])

    rows = read_rows(report)
    assert rows[0] == list(COLUMNS)
    assert len(rows) == 3
    assert sum(1 for row in rows if row == list(COLUMNS)) == 1


def test_append_creates_the_parent_directory(tmp_path: Path) -> None:
    report = tmp_path / "nested" / "out" / "report.csv"
    append_rows(report, [BulkRow("a", "Bitcoin", None, None, None, "UNVERIFIED")])
    assert report.exists()


def test_missing_values_become_empty_cells(tmp_path: Path) -> None:
    report = tmp_path / "report.csv"
    append_rows(report, [BulkRow("a", "Bitcoin", None, Decimal("0"), None, "PARTIAL")])

    row = read_rows(report)[1]
    assert row == ["a", "Bitcoin", "", "0.00", "", "PARTIAL"]


def test_existing_file_is_not_truncated(tmp_path: Path) -> None:
    report = tmp_path / "report.csv"
    append_rows(report, [BulkRow("first", "Bitcoin", None, None, None, "VERIFIED")])
    append_rows(report, [BulkRow("second", "Bitcoin", None, None, None, "VERIFIED")])

    wallets = [row[0] for row in read_rows(report)[1:]]
    assert wallets == ["first", "second"]


# -- the run loop --------------------------------------------------------


def test_run_bulk_continues_after_a_failed_wallet(tmp_path: Path) -> None:
    report = tmp_path / "report.csv"
    groups = [
        [AddressEntry(Network.BITCOIN, BTC)],
        [AddressEntry(Network.ETHEREUM, EVM), AddressEntry(Network.BSC, EVM)],
        [AddressEntry(Network.SOLANA, SOL)],
    ]
    analyzer = StubAnalyzer(failing={EVM})

    rows = run_bulk(groups, analyzer, snapshot(), report)

    assert analyzer.calls == [BTC, EVM, SOL]  # third wallet still processed
    assert [row.status for row in rows] == ["VERIFIED", "ERROR", "ERROR", "VERIFIED"]
    assert len(read_rows(report)) == 1 + 4


def test_run_bulk_reports_progress_per_wallet(tmp_path: Path) -> None:
    groups = [
        [AddressEntry(Network.BITCOIN, BTC)],
        [AddressEntry(Network.ETHEREUM, EVM), AddressEntry(Network.BSC, EVM)],
        [AddressEntry(Network.SOLANA, SOL)],
    ]
    seen: list[tuple[int, int, str]] = []

    run_bulk(
        groups,
        StubAnalyzer(),
        snapshot(),
        tmp_path / "report.csv",
        progress=lambda index, total, address: seen.append((index, total, address)),
    )

    assert seen == [(1, 3, BTC), (2, 3, EVM), (3, 3, SOL)]


def test_run_bulk_reuses_one_price_snapshot_for_every_wallet(tmp_path: Path) -> None:
    analyzer = StubAnalyzer()
    shared = snapshot()
    groups = [
        [AddressEntry(Network.BITCOIN, BTC)],
        [AddressEntry(Network.SOLANA, SOL)],
    ]

    run_bulk(groups, analyzer, shared, tmp_path / "report.csv")

    assert analyzer.prices_seen == [shared, shared]


def test_run_bulk_with_no_groups_writes_nothing(tmp_path: Path) -> None:
    report = tmp_path / "report.csv"
    rows = run_bulk([], StubAnalyzer(), snapshot(), report)

    assert rows == []
    assert not report.exists()
