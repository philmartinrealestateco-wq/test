"""Web-mode selection for the on-chain enrichment pass."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import onchain_scraper
import report_generator
from database import SCHEMA

XRP = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
EVM = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


class WebModeTests(unittest.TestCase):
    def test_all_searches_every_wallet(self):
        self.assertTrue(onchain_scraper.should_search_address("all", None))
        self.assertTrue(
            onchain_scraper.should_search_address(
                "all",
                onchain_scraper.EnsRecord("alice.eth", "", "", ""),
            )
        )

    def test_pivots_skips_address_only(self):
        self.assertFalse(onchain_scraper.should_search_address("pivots", None))
        self.assertTrue(
            onchain_scraper.should_search_address(
                "pivots",
                onchain_scraper.EnsRecord("alice.eth", "", "", ""),
            )
        )

    def test_none_never_searches(self):
        self.assertFalse(onchain_scraper.should_search_address("none", None))
        self.assertFalse(
            onchain_scraper.should_search_address(
                "none",
                onchain_scraper.EnsRecord("alice.eth", "", "", ""),
            )
        )

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            onchain_scraper.should_search_address("bogus", None)

    def test_run_rejects_an_unknown_mode_before_reading_targets(self):
        self.assertEqual(onchain_scraper.run(limit=1, web="bogus"), 2)


class XrpIdentityTests(unittest.TestCase):
    def test_labels_parents_and_twitter_are_parsed(self):
        payload = {
            "accountName": {
                "name": "Binance",
                "desc": "5",
                "account": XRP,
                "domain": "binance.com",
                "twitter": "binance",
                "verified": True,
            },
            "parentName": {
                "name": "Binance",
                "desc": "2",
                "account": "rJb5KsHsDHF1YS5B5DU6QCkH5NsPaKQTcy",
                "domain": "binance.com",
                "twitter": "binance",
                "verified": True,
            },
            "settings": {},
        }

        identity = onchain_scraper.parse_xrp_identity(XRP, payload)
        findings = [row[0] for row in identity.finding_rows()]

        self.assertEqual(identity.label, "Binance")
        self.assertTrue(identity.label_verified)
        self.assertEqual(identity.label_twitter, "binance")
        self.assertEqual(identity.parent_label, "Binance")
        self.assertIn("XRPScan label: Binance (5)", findings)
        self.assertIn("XRPScan twitter: @binance", findings)
        self.assertIn("XRPScan parent label: Binance (2)", findings)

    def test_xls_domain_record_is_decoded(self):
        payload = {
            "accountName": None,
            "parentName": None,
            "settings": {"Domain": b"example.com".hex()},
        }

        identity = onchain_scraper.parse_xrp_identity(XRP, payload)
        findings = [row[0] for row in identity.finding_rows()]

        self.assertEqual(identity.domain_record, "example.com")
        self.assertIn("XRPScan domain record: example.com", findings)

    def test_pivots_search_xrp_only_with_a_domain(self):
        self.assertFalse(
            onchain_scraper.should_search_xrp(
                "pivots", onchain_scraper.XrpIdentity(XRP)
            )
        )
        self.assertTrue(
            onchain_scraper.should_search_xrp(
                "pivots",
                onchain_scraper.XrpIdentity(XRP, domain_record="example.com"),
            )
        )
        self.assertTrue(
            onchain_scraper.should_search_xrp(
                "all", onchain_scraper.XrpIdentity(XRP)
            )
        )
        self.assertFalse(
            onchain_scraper.should_search_xrp(
                "none",
                onchain_scraper.XrpIdentity(XRP, domain_record="example.com"),
            )
        )


class TargetReaderTests(unittest.TestCase):
    def test_read_targets_includes_evm_and_xrp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.csv"
            path.write_text(
                "Wallet,Tokens,Networks,Best Total USD,Status\n"
                f"{EVM},DAI,Ethereum,1000,VERIFIED\n"
                f"{XRP},XRP,XRP,2000,VERIFIED\n"
                "not-an-address,X,?,1,?\n",
                encoding="utf-8",
            )

            targets = onchain_scraper.read_targets(path, None)

        self.assertEqual([target.kind for target in targets], ["evm", "xrp"])
        self.assertEqual(targets[1].address, XRP)


class XrpReportTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.addCleanup(self.db.close)

    def _add_wallet(self, address: str) -> None:
        self.db.execute(
            "INSERT INTO wallets (address, last_scanned) VALUES (?, ?)",
            (address, "2026-11-17 00:00:00"),
        )

    def _add_finding(self, address: str, finding: str, url: str) -> None:
        self.db.execute(
            """
            INSERT INTO osint_findings
                (address, tool, finding, url, confidence, timestamp)
            VALUES (?, 'XRPScan', ?, ?, 'VERIFIED', '2026-11-17 00:00:00')
            """,
            (address, finding, url),
        )

    def test_the_xrp_identity_section_renders_and_makes_the_wallet_eligible(self):
        self._add_wallet(XRP)
        self._add_finding(XRP, "XRPScan label: Binance (5)", "https://binance.com")
        self.db.commit()

        markdown = report_generator.build_markdown(self.db, XRP)
        eligible = report_generator.wallets_with_findings(self.db)

        self.assertIn("## XRP On-Chain Identity", markdown)
        self.assertIn("XRPScan label: Binance (5)", markdown)
        self.assertEqual([row["address"] for row in eligible], [XRP])

    def test_a_case_variant_does_not_pick_up_the_finding(self):
        other = "rHB9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
        self._add_wallet(XRP)
        self._add_wallet(other)
        self._add_finding(XRP, "XRPScan label: Binance (5)", "https://binance.com")
        self.db.commit()

        markdown = report_generator.build_markdown(self.db, other)

        self.assertNotIn("## XRP On-Chain Identity", markdown)


if __name__ == "__main__":
    unittest.main()
