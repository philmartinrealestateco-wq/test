"""CSV and Excel report contents."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest
from openpyxl import load_workbook

from wallet_analyzer.report import (
    COLUMNS,
    format_amount,
    format_timestamp,
    format_usd,
    write_reports,
)


def test_columns_match_the_specification(sample_result, tmp_path) -> None:
    paths = write_reports(sample_result, tmp_path)
    frame = pd.read_csv(paths.all_csv)

    assert list(frame.columns) == COLUMNS
    assert COLUMNS[:10] == [
        "Wallet Address",
        "Blockchain Network",
        "Native Token",
        "Native Token Balance",
        "USD Balance",
        "Last Transaction Date",
        "Days Since Last Transaction",
        "Activity Status",
        "Data Source",
        "Verification Status",
    ]


def test_matches_csv_contains_only_matches(sample_result, tmp_path) -> None:
    paths = write_reports(sample_result, tmp_path)
    frame = pd.read_csv(paths.matches_csv)

    assert len(frame) == 1
    assert frame.iloc[0]["Verification Status"] == "VERIFIED"
    assert frame.iloc[0]["Activity Status"] == "ACTIVE"


def test_matches_are_sorted_by_usd_descending(tmp_path) -> None:
    from tests.factories import make_record, make_result

    records = [
        make_record(address="a", usd_value=Decimal("60000")),
        make_record(address="b", usd_value=Decimal("800000")),
        make_record(address="c", usd_value=Decimal("120000")),
    ]
    result = make_result(records)
    paths = write_reports(result, tmp_path)
    frame = pd.read_csv(paths.matches_csv)

    assert list(frame["USD Balance"]) == ["800,000.00", "120,000.00", "60,000.00"]


def test_workbook_has_expected_sheets(sample_result, tmp_path) -> None:
    paths = write_reports(sample_result, tmp_path)
    workbook = load_workbook(paths.workbook)

    assert workbook.sheetnames == [
        "Matches",
        "All Addresses",
        "Summary",
        "Network Distribution",
    ]
    assert workbook["Matches"].freeze_panes == "A2"
    assert workbook["Matches"].auto_filter.ref is not None


def test_report_never_contains_credentials(sample_result, tmp_path) -> None:
    paths = write_reports(sample_result, tmp_path)
    for path in paths.as_list():
        if path.suffix == ".xlsx":
            continue
        text = path.read_text(encoding="utf-8-sig")
        assert "apikey" not in text.lower()
        assert "api_key" not in text.lower()


def test_empty_result_still_writes_headers(tmp_path) -> None:
    from tests.factories import make_result

    paths = write_reports(make_result([]), tmp_path)
    frame = pd.read_csv(paths.all_csv)
    assert frame.empty
    assert list(frame.columns) == COLUMNS


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (Decimal("0"), "0"),
        (Decimal("1.50000000"), "1.5"),
        (Decimal("157.01008336931"), "157.01008336931"),
    ],
)
def test_format_amount(value, expected) -> None:
    assert format_amount(value) == expected


def test_format_helpers() -> None:
    assert format_usd(Decimal("89529.6")) == "89,529.60"
    assert format_usd(None) == ""
    assert format_timestamp(None) == ""


# -- BEP-20 columns ------------------------------------------------------


def test_token_columns_are_populated(tmp_path) -> None:
    from datetime import datetime, timezone

    from tests.factories import make_result
    from wallet_analyzer.models import (
        ActivityStatus,
        Network,
        TokenHolding,
        VerificationStatus,
        WalletRecord,
    )

    record = WalletRecord(
        address="0xBSC",
        network=Network.BSC,
        balance=Decimal("1"),
        price_usd=Decimal("686.12"),
        usd_value=Decimal("686.12"),
        tokens_usd=Decimal("250"),
        tokens_checked=True,
        token_holdings=[
            TokenHolding(
                contract="0x55d398326f99059fF775485246999027B3197955",
                symbol="USDT",
                decimals=18,
                raw_balance=250 * 10**18,
                amount=Decimal("250"),
                price_usd=Decimal("1"),
                usd_value=Decimal("250"),
            )
        ],
        token_note="12 token contract(s) checked; unlisted tokens are not detected",
        activity_status=ActivityStatus.UNKNOWN,
        verification_status=VerificationStatus.PARTIAL,
        data_source="test",
        last_tx_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
        days_since_last_tx=1,
    )
    paths = write_reports(make_result([record], matches=[]), tmp_path)
    frame = pd.read_csv(paths.all_csv)
    row = frame.iloc[0]
    raw = paths.all_csv.read_text(encoding="utf-8-sig")

    assert "250.00" in raw and "936.12" in raw  # formatted as currency in the CSV
    assert float(row["Token Holdings (USD)"]) == 250.0
    assert float(row["Total USD"]) == 936.12
    assert row["Token Count"] == 1
    assert "BEP-20 holdings: USDT 250" in row["Notes"]
    assert "unlisted tokens are not detected" in row["Notes"]


def test_token_columns_are_blank_when_tokens_were_not_checked(sample_result, tmp_path) -> None:
    paths = write_reports(sample_result, tmp_path)
    frame = pd.read_csv(paths.all_csv)

    assert frame["Token Holdings (USD)"].isna().all()
    assert frame["Token Count"].isna().all()
    # Total USD falls back to the native value when no token scan happened.
    assert not frame["Total USD"].isna().all()
