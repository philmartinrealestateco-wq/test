"""CSV and Excel output.

The CSV is the precision-preserving artifact; the workbook is the presentation
one (numeric cells, formatting, extra summary sheets).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .models import (
    ActivityStatus,
    AnalysisResult,
    Network,
    VerificationStatus,
    WalletRecord,
)

COLUMNS = [
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
    "Token Holdings (USD)",
    "Total USD",
    "Token Count",
    "Notes",
]

_HEADER_FILL = PatternFill("solid", fgColor="1F2937")
_HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
_HEADER_ALIGN = Alignment(horizontal="left", vertical="center")
_THIN = Side(style="thin", color="D1D5DB")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

_WIDTHS = {
    "Wallet Address": 48,
    "Blockchain Network": 18,
    "Native Token": 8,
    "Native Token Balance": 24,
    "USD Balance": 16,
    "Last Transaction Date": 22,
    "Days Since Last Transaction": 14,
    "Activity Status": 12,
    "Data Source": 46,
    "Verification Status": 16,
    "Token Holdings (USD)": 20,
    "Total USD": 16,
    "Token Count": 12,
    "Notes": 64,
}


@dataclass(frozen=True)
class ReportPaths:
    all_csv: Path
    matches_csv: Path
    workbook: Path

    def as_list(self) -> list[Path]:
        return [self.matches_csv, self.all_csv, self.workbook]


def format_amount(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_usd(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{value:,.2f}"


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_to_row(record: WalletRecord) -> dict[str, object]:
    return {
        "Wallet Address": record.address,
        "Blockchain Network": record.network.label,
        "Native Token": record.native_symbol,
        "Native Token Balance": format_amount(record.balance),
        "USD Balance": format_usd(record.usd_value),
        "Last Transaction Date": format_timestamp(record.last_tx_at),
        "Days Since Last Transaction": (
            "" if record.days_since_last_tx is None else record.days_since_last_tx
        ),
        "Activity Status": record.activity_status.value,
        "Data Source": record.data_source,
        "Verification Status": record.verification_status.value,
        "Token Holdings (USD)": format_usd(record.tokens_usd),
        "Total USD": format_usd(record.total_usd),
        "Token Count": record.token_count if record.tokens_checked else "",
        "Notes": _notes_with_tokens(record),
    }


def _notes_with_tokens(record: WalletRecord) -> str:
    notes = [note for note in record.notes if note]
    if record.tokens_checked and record.token_holdings:
        symbols = ", ".join(
            f"{holding.symbol} {format_amount(holding.amount)}" for holding in record.token_holdings[:6]
        )
        if len(record.token_holdings) > 6:
            symbols += f", +{len(record.token_holdings) - 6} more"
        notes.append(f"BEP-20 holdings: {symbols}")
    if record.tokens_checked and record.token_note:
        notes.append(record.token_note)
    return "; ".join(notes)


def build_frame(records: list[WalletRecord], *, for_excel: bool = False) -> pd.DataFrame:
    # The report is always ordered by value; ordering must not depend on how the
    # caller happened to collect the records.
    ordered = sorted(records, key=_usd_sort_key, reverse=True)
    rows = [record_to_row(record) for record in ordered]
    frame = pd.DataFrame(rows, columns=COLUMNS)
    if for_excel and not frame.empty:
        frame["USD Balance"] = [
            float(record.usd_value) if record.usd_value is not None else None
            for record in ordered
        ]
        frame["Native Token Balance"] = [
            float(record.balance) if record.balance is not None else None
            for record in ordered
        ]
        frame["Days Since Last Transaction"] = [
            record.days_since_last_tx for record in ordered
        ]
        frame["Token Holdings (USD)"] = [
            float(record.tokens_usd) if record.tokens_usd is not None else None
            for record in ordered
        ]
        frame["Total USD"] = [
            float(record.total_usd) if record.total_usd is not None else None
            for record in ordered
        ]
        frame["Token Count"] = [
            record.token_count if record.tokens_checked else None for record in ordered
        ]
    return frame


def _usd_sort_key(record: WalletRecord) -> tuple[bool, Decimal]:
    return (record.total_usd is not None, record.total_usd or Decimal(0))


def write_reports(
    result: AnalysisResult,
    out_dir: Path,
    *,
    timestamp: datetime | None = None,
) -> ReportPaths:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (timestamp or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")

    all_csv = out_dir / f"wallet_analysis_{stamp}.csv"
    matches_csv = out_dir / f"wallet_matches_{stamp}.csv"
    workbook = out_dir / f"wallet_analysis_{stamp}.xlsx"

    build_frame(result.records).to_csv(all_csv, index=False, encoding="utf-8-sig")
    build_frame(result.matches).to_csv(matches_csv, index=False, encoding="utf-8-sig")
    _write_workbook(result, workbook)

    return ReportPaths(all_csv=all_csv, matches_csv=matches_csv, workbook=workbook)


def _write_workbook(result: AnalysisResult, path: Path) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        build_frame(result.matches, for_excel=True).to_excel(
            writer, sheet_name="Matches", index=False
        )
        build_frame(result.records, for_excel=True).to_excel(
            writer, sheet_name="All Addresses", index=False
        )
        pd.DataFrame(_summary_rows(result), columns=["Metric", "Value"]).to_excel(
            writer, sheet_name="Summary", index=False
        )
        pd.DataFrame(
            _distribution_rows(result),
            columns=["Network", "Wallets", "Verified", "Active", "Matches"],
        ).to_excel(writer, sheet_name="Network Distribution", index=False)

    _style_workbook(path)


def _style_workbook(path: Path) -> None:
    from openpyxl import load_workbook

    workbook = load_workbook(path)
    for sheet in workbook.worksheets:
        header_cells = list(sheet.iter_rows(min_row=1, max_row=1))
        for cell_row in header_cells:
            for cell in cell_row:
                cell.fill = _HEADER_FILL
                cell.font = _HEADER_FONT
                cell.alignment = _HEADER_ALIGN
                cell.border = _BORDER

        if sheet.max_row > 1:
            sheet.auto_filter.ref = sheet.dimensions
        sheet.freeze_panes = "A2"

        headers = [cell.value for cell in sheet[1]]
        for index, header in enumerate(headers, start=1):
            letter = get_column_letter(index)
            sheet.column_dimensions[letter].width = _WIDTHS.get(
                str(header), 18 if sheet.title != "Summary" else 34
            )

        if sheet.title in {"Matches", "All Addresses"}:
            money_columns = {
                headers.index(name) + 1
                for name in ("USD Balance", "Token Holdings (USD)", "Total USD")
                if name in headers
            }
            balance_index = (
                headers.index("Native Token Balance") + 1
                if "Native Token Balance" in headers
                else None
            )
            notes_index = headers.index("Notes") + 1 if "Notes" in headers else None
            for row in sheet.iter_rows(min_row=2):
                for cell in row:
                    cell.border = _BORDER
                    if cell.column in money_columns:
                        cell.number_format = "#,##0.00"
                    elif balance_index and cell.column == balance_index:
                        cell.number_format = "#,##0.00000000"
                    elif notes_index and cell.column == notes_index:
                        cell.alignment = Alignment(wrap_text=True, vertical="top")

    workbook.save(path)


def _summary_rows(result: AnalysisResult) -> list[tuple[str, object]]:
    statuses = {status: 0 for status in VerificationStatus}
    for record in result.records:
        statuses[record.verification_status] += 1

    rows: list[tuple[str, object]] = [
        ("Generated at (UTC)", format_timestamp(result.finished_at)),
        ("Analysis duration (s)", f"{(result.finished_at - result.started_at).total_seconds():.1f}"),
        ("Balance range (USD)", f"{result.settings.min_usd:,.0f} - {result.settings.max_usd:,.0f}"),
        ("Activity window (days)", result.settings.activity_days),
        ("Addresses analysed", result.total_addresses),
        ("Fully verified", statuses[VerificationStatus.VERIFIED]),
        ("Partially verified (balance only)", statuses[VerificationStatus.PARTIAL]),
        ("Unverified", statuses[VerificationStatus.UNVERIFIED]),
        ("Invalid addresses", statuses[VerificationStatus.INVALID]),
        ("Active wallets", result.active_count),
        ("Wallets inside balance range", result.in_range_count),
        ("Matching wallets", len(result.matches)),
        ("Total USD of matches", float(result.matched_total_usd)),
        ("Wallets holding BEP-20", result.wallets_with_tokens),
        ("BEP-20 value (USD)", float(result.token_value_usd)),
        ("Price source", result.price_source),
        ("Prices as of (UTC)", format_timestamp(result.price_fetched_at)),
    ]
    for asset, price in result.prices.items():
        rows.append((f"{asset} price (USD)", float(price)))
    return rows


def _distribution_rows(result: AnalysisResult) -> list[tuple[str, int, int, int, int]]:
    rows: list[tuple[str, int, int, int, int]] = []
    for network, total in result.network_distribution().items():
        network_records = [record for record in result.records if record.network is network]
        rows.append(
            (
                network.label,
                total,
                sum(
                    1
                    for record in network_records
                    if record.verification_status is VerificationStatus.VERIFIED
                ),
                sum(
                    1
                    for record in network_records
                    if record.activity_status is ActivityStatus.ACTIVE
                ),
                sum(1 for record in result.matches if record.network is network),
            )
        )
    return rows
