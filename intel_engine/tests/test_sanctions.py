"""Tests for the OpenSanctions screening phase.

Run with the intel_engine interpreter:
    intel_engine\\.venv\\Scripts\\python.exe -m unittest discover -s intel_engine\\tests
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import report_generator
import sanctions
from database import SCHEMA

EVM_ADDRESS = "0xE950DC316b836e4EeFb8308bf32Bf7C72a1358FF"
TRON_ADDRESS = "TGdpkwNVFjw2DnbHBCFKLvCygPVPz9w4iM"
HOLDER_ID = "ofac-abc123"
WALLET_ID = "ofac-def456"


def wallet_entity(address: str, currency: str = "ETH") -> dict:
    return {
        "id": WALLET_ID,
        "caption": address,
        "schema": "CryptoWallet",
        "datasets": ["us_ofac_sdn"],
        "properties": {
            "publicKey": [address],
            "currency": [currency],
            "holder": [HOLDER_ID],
            "topics": ["sanction"],
        },
        "target": True,
    }


def holder_entity(caption: str = "ACME BANK") -> dict:
    return {"id": HOLDER_ID, "caption": caption, "schema": "Organization", "properties": {}}


def write_export(directory: Path, entities: list[dict]) -> Path:
    path = directory / "entities.ftm.json"
    path.write_text("\n".join(json.dumps(entity) for entity in entities) + "\n", encoding="utf-8")
    return path


def memory_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


class AddressKeyTests(unittest.TestCase):
    def test_evm_addresses_compare_case_insensitively(self):
        self.assertEqual(sanctions.address_key(EVM_ADDRESS), EVM_ADDRESS.lower())
        self.assertEqual(
            sanctions.address_key(EVM_ADDRESS.lower()), sanctions.address_key(EVM_ADDRESS.upper().replace("0X", "0x"))
        )

    def test_base58_addresses_keep_their_case(self):
        self.assertEqual(sanctions.address_key(TRON_ADDRESS), TRON_ADDRESS)
        self.assertNotEqual(sanctions.address_key(TRON_ADDRESS), TRON_ADDRESS.lower())

    def test_short_hex_strings_are_not_treated_as_evm(self):
        self.assertEqual(sanctions.address_key("0xABCD"), "0xABCD")


class ReadWalletsTests(unittest.TestCase):
    def test_collects_wallets_and_resolves_the_holder_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_export(Path(directory), [holder_entity(), wallet_entity(EVM_ADDRESS)])

            wallets, entities = sanctions.read_wallets(path)

        self.assertEqual(entities, 2)
        self.assertEqual(len(wallets), 1)
        self.assertEqual(wallets[0].address, EVM_ADDRESS)
        self.assertEqual(wallets[0].currency, "ETH")
        self.assertEqual(wallets[0].holder, "ACME BANK")
        self.assertEqual(wallets[0].entity_id, WALLET_ID)

    def test_an_export_without_wallets_is_an_error_not_zero_hits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_export(Path(directory), [holder_entity()])

            with self.assertRaises(sanctions.SanctionsError):
                sanctions.read_wallets(path)

    def test_invalid_json_names_the_problem(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "entities.ftm.json"
            path.write_text("{not json}\n", encoding="utf-8")

            with self.assertRaises(sanctions.SanctionsError):
                sanctions.read_wallets(path)


class MatchTests(unittest.TestCase):
    def dataset(self, record: sanctions.SanctionedWallet) -> sanctions.DatasetInfo:
        return sanctions.DatasetInfo(
            name="us_ofac_sdn",
            updated="2026-11-13",
            source_url="https://example.invalid/entities.ftm.json",
            size=1,
            path=Path("unused"),
            entities=2,
            wallets=1,
            records=[record],
        )

    def record(self, address: str, currency: str = "ETH") -> sanctions.SanctionedWallet:
        return sanctions.SanctionedWallet(
            address=address, currency=currency, holder="ACME BANK", entity_id=WALLET_ID
        )

    def test_evm_case_variants_match(self):
        dataset = self.dataset(self.record(EVM_ADDRESS))
        matches = sanctions.match_targets([EVM_ADDRESS.lower()], dataset)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0], EVM_ADDRESS.lower())

    def test_base58_case_difference_does_not_match(self):
        dataset = self.dataset(self.record(TRON_ADDRESS, currency="USDT"))
        self.assertEqual(sanctions.match_targets([TRON_ADDRESS.lower()], dataset), [])
        self.assertEqual(len(sanctions.match_targets([TRON_ADDRESS], dataset)), 1)

    def test_finding_text_carries_holder_and_currency(self):
        text = sanctions.finding_text(self.record(EVM_ADDRESS), "us_ofac_sdn")
        self.assertEqual(text, "us_ofac_sdn listing: ACME BANK (ETH)")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)
        self.wallet = sanctions.SanctionedWallet(
            address=EVM_ADDRESS, currency="ETH", holder="ACME BANK", entity_id=WALLET_ID
        )
        self.dataset = sanctions.DatasetInfo(
            name="us_ofac_sdn",
            updated="2026-11-13",
            source_url="https://example.invalid/entities.ftm.json",
            size=1,
            path=Path("unused"),
            entities=2,
            wallets=1,
            records=[self.wallet],
        )

    def test_storing_twice_keeps_one_row(self):
        first = sanctions.store_matches(self.db, [(EVM_ADDRESS, self.wallet)], "us_ofac_sdn")
        second = sanctions.store_matches(self.db, [(EVM_ADDRESS, self.wallet)], "us_ofac_sdn")

        rows = self.db.execute(
            "SELECT tool, finding, url, confidence FROM osint_findings"
        ).fetchall()

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "OpenSanctions")
        self.assertEqual(rows[0]["confidence"], "LIST HIT")
        self.assertEqual(rows[0]["url"], f"https://www.opensanctions.org/entities/{WALLET_ID}/")

    def test_a_zero_match_run_is_recorded(self):
        sanctions.record_run(self.db, self.dataset, checked=246, matches=0)

        run = report_generator.load_sanctions_runs(self.db)[0]
        self.assertEqual(run["checked"], 246)
        self.assertEqual(run["matches"], 0)
        self.assertEqual(run["wallets"], 1)

    def test_the_latest_run_wins(self):
        sanctions.record_run(self.db, self.dataset, checked=10, matches=0)
        sanctions.record_run(self.db, self.dataset, checked=246, matches=1)

        runs = report_generator.load_sanctions_runs(self.db)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["checked"], 246)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)
        self.db.execute(
            "INSERT INTO wallets (address, ens_name, last_scanned) VALUES (?, ?, ?)",
            (EVM_ADDRESS.lower(), None, "2026-11-16 10:00 UTC"),
        )
        self.db.commit()
        self.wallet = sanctions.SanctionedWallet(
            address=EVM_ADDRESS, currency="ETH", holder="ACME BANK", entity_id=WALLET_ID
        )
        self.dataset = sanctions.DatasetInfo(
            name="us_ofac_sdn",
            updated="2026-11-13",
            source_url="https://example.invalid/entities.ftm.json",
            size=1,
            path=Path("unused"),
            entities=2,
            wallets=1,
            records=[self.wallet],
        )

    def test_a_list_hit_raises_an_alert_and_links_the_entity(self):
        sanctions.store_matches(self.db, [(EVM_ADDRESS.lower(), self.wallet)], "us_ofac_sdn")
        sanctions.record_run(self.db, self.dataset, checked=246, matches=1)

        markdown = report_generator.build_markdown(self.db, EVM_ADDRESS.lower())

        self.assertIn("## Sanctions Screening", markdown)
        self.assertIn("Listed wallet.", markdown)
        self.assertIn(f"https://www.opensanctions.org/entities/{WALLET_ID}/", markdown)
        self.assertIn("ACME BANK", markdown)

    def test_no_findings_still_states_what_was_checked(self):
        sanctions.record_run(self.db, self.dataset, checked=246, matches=0)

        markdown = report_generator.build_markdown(self.db, EVM_ADDRESS.lower())

        self.assertIn("_No sanctions listing matched this address._", markdown)
        self.assertIn("246 cached wallet(s)", markdown)
        self.assertIn("absence from a sanctions list is not evidence", markdown)

    def test_an_unrun_screen_is_labelled_as_such(self):
        markdown = report_generator.build_markdown(self.db, EVM_ADDRESS.lower())
        self.assertIn("No sanctions screening has been recorded", markdown)

    def test_a_list_hit_alone_makes_the_wallet_reportable(self):
        sanctions.store_matches(self.db, [(EVM_ADDRESS.lower(), self.wallet)], "us_ofac_sdn")
        eligible = [row["address"] for row in report_generator.wallets_with_findings(self.db)]
        self.assertEqual(eligible, [EVM_ADDRESS.lower()])


if __name__ == "__main__":
    unittest.main()
