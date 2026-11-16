"""Tests for the FlowLens adapter, evidence storage and the tracing section."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flowlens
import report_generator
from adapters.flowlens_adapter import (
    FlowLensError,
    format_amount,
    load_case,
    summarise,
)
from database import SCHEMA

ADDRESS = "0x18e296053cbdf986196903e889b7dca7a73882f6"
OTHER = "0xc78ddcdb9e90a02ec6ace446037c6079d773efd6"
TX = "0x0a71c2833038602ce6f9e07c98e7af023d8db1f6b50c026ab44083ea46260f1d"


def case_payload(transfers: list[dict] | None = None, coverage: list[dict] | None = None) -> dict:
    return {
        "version": 1,
        "root": ADDRESS,
        "mode": "live",
        "updatedAt": "2026-11-16T04:00:00.000Z",
        "transfers": transfers if transfers is not None else [transfer()],
        "coverage": coverage
        if coverage is not None
        else [
            {
                "provider": "blockscout-v2",
                "status": "partial",
                "pagesLoaded": 1,
                "chainId": 1,
                "address": ADDRESS,
            }
        ],
        "nodes": [],
    }


def transfer(direction: str = "out", raw: str = "11000000") -> dict:
    sender, recipient = (ADDRESS, OTHER) if direction == "out" else (OTHER, ADDRESS)
    return {
        "chainId": 1,
        "hash": TX,
        "eventIndex": "14",
        "from": sender,
        "to": recipient,
        "asset": {"symbol": "USDT", "decimals": 6, "contract": "0xdac1"},
        "rawAmount": raw,
        "timestamp": "2026-11-16T03:04:35.000Z",
        "blockNumber": 26424775,
        "kind": "erc20",
        "source": "Blockscout API v2 (eth.blockscout.com)",
    }


def write_case(directory: str, payload: dict) -> Path:
    path = Path(directory) / "case.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def memory_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


class AmountTests(unittest.TestCase):
    def test_six_decimals_are_applied(self):
        self.assertEqual(format_amount("11000000", 6), "11")
        self.assertEqual(format_amount("1500000", 6), "1.5")

    def test_eighteen_decimals_are_applied(self):
        self.assertEqual(format_amount("1000000000000000000", 18), "1")

    def test_missing_decimals_leave_the_raw_value(self):
        self.assertEqual(format_amount("42", None), "42")

    def test_garbage_is_not_silently_zeroed(self):
        self.assertEqual(format_amount("not-a-number", 6), "not-a-number")


class LoadCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_outgoing_transfer_is_attributed_to_the_counterparty(self):
        case = load_case(write_case(self.tmp.name, case_payload()))
        self.assertEqual(len(case.transfers), 1)
        row = case.transfers[0]
        self.assertEqual(row.direction, "out")
        self.assertEqual(row.counterparty, OTHER)
        self.assertEqual(row.amount, "11")
        self.assertEqual(row.asset, "USDT")
        self.assertEqual(row.source_url, f"https://etherscan.io/tx/{TX}")

    def test_incoming_transfer_is_marked_in(self):
        payload = case_payload(transfers=[transfer("in")])
        case = load_case(write_case(self.tmp.name, payload))
        self.assertEqual(case.transfers[0].direction, "in")

    def test_unrelated_transfers_are_skipped_and_counted(self):
        unrelated = transfer()
        unrelated["from"] = OTHER
        unrelated["to"] = "0x" + "1" * 40
        payload = case_payload(transfers=[transfer(), unrelated])
        case = load_case(write_case(self.tmp.name, payload))
        self.assertEqual(len(case.transfers), 1)
        self.assertEqual(case.skipped, 1)

    def test_partial_coverage_is_reported(self):
        case = load_case(write_case(self.tmp.name, case_payload()))
        self.assertTrue(case.partial)
        self.assertIn("blockscout-v2 partial, 1 page(s)", case.coverage_note())

    def test_complete_coverage_is_not_partial(self):
        payload = case_payload(
            coverage=[{"provider": "blockscout-v2", "status": "complete", "pagesLoaded": 3}]
        )
        case = load_case(write_case(self.tmp.name, payload))
        self.assertFalse(case.partial)

    def test_invalid_json_is_an_error(self):
        path = Path(self.tmp.name) / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(FlowLensError):
            load_case(path)

    def test_a_case_with_neither_transfers_nor_coverage_is_an_error(self):
        payload = case_payload(transfers=[], coverage=[])
        with self.assertRaises(FlowLensError):
            load_case(write_case(self.tmp.name, payload))

    def test_an_explicit_address_overrides_the_case_root(self):
        payload = case_payload(transfers=[transfer("in")])
        path = write_case(self.tmp.name, payload)
        case = load_case(path, address=OTHER)
        self.assertEqual(case.transfers[0].direction, "out")


class SummaryTests(unittest.TestCase):
    def test_totals_are_grouped_per_counterparty_and_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = case_payload(
                transfers=[
                    transfer("out", raw="11000000"),
                    transfer("out", raw="4000000"),
                    transfer("in", raw="5000000"),
                ]
            )
            case = load_case(write_case(directory, payload))

        summary = summarise(case)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["count"], 3)
        self.assertEqual(summary[0]["in"], "5")
        self.assertEqual(summary[0]["out"], "15")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = load_case(write_case(self.tmp.name, case_payload()))

    def test_rows_are_stored_with_coverage(self):
        stored = flowlens.store_case(self.db, self.case)
        row = self.db.execute("SELECT * FROM flow_evidence").fetchone()

        self.assertEqual(stored, 1)
        self.assertEqual(row["tx_hash"], TX)
        self.assertEqual(row["direction"], "out")
        self.assertEqual(row["asset"], "USDT")
        self.assertEqual(row["confidence"], "VERIFIED")
        self.assertIn("partial", row["coverage"])

    def test_reimporting_replaces_instead_of_duplicating(self):
        flowlens.store_case(self.db, self.case)
        flowlens.store_case(self.db, self.case)
        count = self.db.execute("SELECT count(*) FROM flow_evidence").fetchone()[0]
        self.assertEqual(count, 1)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)
        self.db.execute(
            "INSERT INTO wallets (address, ens_name, last_scanned) VALUES (?, ?, ?)",
            (ADDRESS, None, "2026-11-16 12:00 UTC"),
        )
        self.db.commit()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = load_case(write_case(self.tmp.name, case_payload()))
        flowlens.store_case(self.db, self.case)

    def test_the_section_lists_coverage_counterparties_and_the_transaction(self):
        markdown = report_generator.build_markdown(self.db, ADDRESS)

        self.assertIn("## Transaction Tracing & Funding", markdown)
        self.assertIn("blockscout-v2 partial", markdown)
        self.assertIn("A partial trace is a bounded window", markdown)
        self.assertIn("| Counterparty | Asset | Transfers | In | Out |", markdown)
        self.assertIn(f"https://etherscan.io/tx/{TX}", markdown)
        self.assertIn("USDT", markdown)
        self.assertIn("1 transfer(s) loaded (0 in, 1 out)", markdown)

    def test_an_untraced_wallet_says_so(self):
        self.db.execute("DELETE FROM flow_evidence")
        self.db.commit()

        markdown = report_generator.build_markdown(self.db, ADDRESS)

        self.assertIn("_No on-chain flow traced for this address._", markdown)
        self.assertIn("was not reviewed here", markdown)

    def test_tracing_alone_makes_the_wallet_reportable(self):
        eligible = [row["address"] for row in report_generator.wallets_with_findings(self.db)]
        self.assertEqual(eligible, [ADDRESS])


if __name__ == "__main__":
    unittest.main()
