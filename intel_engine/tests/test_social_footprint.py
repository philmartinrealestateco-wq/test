"""Tests for the Maigret adapter, URL corroboration and the footprint table.

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

import osint_runner
import report_generator
from adapters import canonical_url, maigret_adapter
from database import SCHEMA

ADDRESS = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"


def memory_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def add_finding(db, tool: str, url: str, site: str = "Facebook", confidence: str = "UNVERIFIED") -> None:
    db.execute(
        "INSERT INTO osint_findings (address, tool, finding, url, confidence, timestamp)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ADDRESS, tool, f"Account on {site}", url, confidence, "2026-11-16 12:00 UTC"),
    )
    db.commit()


class CanonicalUrlTests(unittest.TestCase):
    def test_casing_and_trailing_slash_fold_together(self):
        self.assertEqual(
            canonical_url("https://www.facebook.com/VitalikButerin/"),
            canonical_url("https://facebook.com/vitalikbuterin"),
        )

    def test_separators_in_the_username_fold_together(self):
        self.assertEqual(
            canonical_url("https://www.facebook.com/VitalikButerin"),
            canonical_url("https://www.facebook.com/vitalik.buterin"),
        )

    def test_different_hosts_stay_apart(self):
        self.assertNotEqual(
            canonical_url("https://github.com/vitalik"),
            canonical_url("https://gitlab.com/vitalik"),
        )

    def test_different_query_identifiers_stay_apart(self):
        self.assertNotEqual(
            canonical_url("https://example.com/profile.php?id=1"),
            canonical_url("https://example.com/profile.php?id=2"),
        )

    def test_empty_values_are_safe(self):
        self.assertEqual(canonical_url(None), "")
        self.assertEqual(canonical_url("  "), "")


class ParseReportTests(unittest.TestCase):
    def write_report(self, lines: list[str]) -> Path:
        directory = tempfile.mkdtemp()
        path = Path(directory) / "report_vitalikbuterin_ndjson.json"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_claimed_profiles_are_returned(self):
        path = self.write_report(
            [
                json.dumps(
                    {
                        "sitename": "Facebook",
                        "status": {
                            "status": "Claimed",
                            "url": "https://www.facebook.com/VitalikButerin",
                            "site_name": "Facebook",
                        },
                    }
                )
            ]
        )
        accounts = maigret_adapter.parse_report(path)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].site, "Facebook")
        self.assertEqual(accounts[0].url, "https://www.facebook.com/VitalikButerin")
        self.assertEqual(accounts[0].variant, "vitalikbuterin")

    def test_string_status_is_understood(self):
        path = self.write_report(
            [json.dumps({"sitename": "GitHub", "status": "Claimed", "url_user": "https://github.com/vitalik"})]
        )
        self.assertEqual(len(maigret_adapter.parse_report(path)), 1)

    def test_unclaimed_profiles_are_dropped(self):
        path = self.write_report(
            [
                json.dumps({"sitename": "GitHub", "status": {"status": "Available"}, "url_user": "https://github.com/nobody"}),
                json.dumps({"sitename": "GitHub", "status": {"status": "Unknown"}, "url_user": "https://github.com/maybe"}),
            ]
        )
        self.assertEqual(maigret_adapter.parse_report(path), [])

    def test_the_same_profile_in_two_forms_is_deduplicated(self):
        path = self.write_report(
            [
                json.dumps({"sitename": "Facebook", "status": {"status": "Claimed"}, "url_user": "https://www.facebook.com/VitalikButerin"}),
                json.dumps({"sitename": "Facebook", "status": {"status": "Claimed"}, "url_user": "https://facebook.com/vitalik.buterin/"}),
            ]
        )
        self.assertEqual(len(maigret_adapter.parse_report(path)), 1)

    def test_a_corrupt_line_does_not_lose_the_good_ones(self):
        path = self.write_report(
            [
                "not json at all",
                json.dumps({"sitename": "GitHub", "status": {"status": "Claimed"}, "url_user": "https://github.com/vitalik"}),
            ]
        )
        self.assertEqual(len(maigret_adapter.parse_report(path)), 1)

    def test_the_report_is_empty_input_not_an_error(self):
        path = self.write_report([])
        self.assertEqual(maigret_adapter.parse_report(path), [])


class FindReportsTests(unittest.TestCase):
    def test_every_variant_report_is_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first = folder / "report_VitalikButerin_ndjson.json"
            second = folder / "report_vitalik_ndjson.json"
            first.write_text("", encoding="utf-8")
            second.write_text("", encoding="utf-8")

            self.assertEqual(
                {path.name for path in maigret_adapter.find_reports(folder)},
                {first.name, second.name},
            )

    def test_no_report_returns_an_empty_list(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(maigret_adapter.find_reports(Path(directory)), [])


class MergeReportsTests(unittest.TestCase):
    def test_variants_are_merged_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            facebook = json.dumps(
                {"sitename": "Facebook", "status": {"status": "Claimed"}, "url_user": "https://www.facebook.com/VitalikButerin"}
            )
            facebook_variant = json.dumps(
                {"sitename": "Facebook", "status": {"status": "Claimed"}, "url_user": "https://facebook.com/vitalik.buterin"}
            )
            soundcloud = json.dumps(
                {"sitename": "SoundCloud", "status": {"status": "Claimed"}, "url_user": "https://soundcloud.com/vitalik"}
            )
            (folder / "report_VitalikButerin_ndjson.json").write_text(facebook + "\n", encoding="utf-8")
            (folder / "report_vitalik_ndjson.json").write_text(
                facebook_variant + "\n" + soundcloud + "\n", encoding="utf-8"
            )

            accounts, raw = maigret_adapter.parse_reports(maigret_adapter.find_reports(folder))

        self.assertEqual(len(accounts), 2)
        self.assertEqual(raw, 3)
        self.assertEqual({account.site for account in accounts}, {"Facebook", "SoundCloud"})


class AdapterFailureTests(unittest.TestCase):
    def test_a_missing_executable_is_an_error_not_zero_accounts(self):
        result = maigret_adapter.run("vitalik", executable=Path("does-not-exist.exe"))
        self.assertFalse(result.ok)
        self.assertIn("not found", result.error)
        self.assertEqual(result.accounts, [])

    def test_a_handle_without_a_report_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "maigret.exe"
            fake.write_text("@echo off\nexit /b 0\n", encoding="utf-8")
            result = maigret_adapter.run("vitalik", timeout=30, executable=fake)
        self.assertFalse(result.ok)
        self.assertEqual(result.accounts, [])


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)

    def test_two_tools_on_the_same_profile_become_corroborated(self):
        add_finding(self.db, "Sherlock", "https://www.facebook.com/VitalikButerin")
        add_finding(self.db, "Maigret", "https://www.facebook.com/vitalik.buterin")

        promoted = osint_runner.reconcile_social_findings(self.db, ADDRESS)

        rows = self.db.execute("SELECT confidence FROM osint_findings").fetchall()
        self.assertEqual(promoted, 2)
        self.assertEqual({row["confidence"] for row in rows}, {"CORROBORATED"})

    def test_a_single_tool_stays_unverified(self):
        add_finding(self.db, "Sherlock", "https://github.com/vitalik")

        promoted = osint_runner.reconcile_social_findings(self.db, ADDRESS)

        row = self.db.execute("SELECT confidence FROM osint_findings").fetchone()
        self.assertEqual(promoted, 0)
        self.assertEqual(row["confidence"], "UNVERIFIED")

    def test_different_profiles_are_not_corroborated_together(self):
        add_finding(self.db, "Sherlock", "https://github.com/vitalik")
        add_finding(self.db, "Maigret", "https://gitlab.com/vitalik")

        self.assertEqual(osint_runner.reconcile_social_findings(self.db, ADDRESS), 0)

    def test_other_tools_do_not_count_as_agreement(self):
        add_finding(self.db, "Sherlock", "https://github.com/vitalik")
        add_finding(self.db, "DuckDuckGo", "https://github.com/vitalik", site="x")

        self.assertEqual(osint_runner.reconcile_social_findings(self.db, ADDRESS), 0)


class FootprintReportTests(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)
        self.db.execute(
            "INSERT INTO wallets (address, ens_name, last_scanned) VALUES (?, ?, ?)",
            (ADDRESS, None, "2026-11-16 12:00 UTC"),
        )
        self.db.commit()

    def test_the_table_lists_both_tools_and_the_promoted_confidence(self):
        add_finding(self.db, "Sherlock", "https://www.facebook.com/VitalikButerin")
        add_finding(self.db, "Maigret", "https://www.facebook.com/vitalik.buterin")
        osint_runner.reconcile_social_findings(self.db, ADDRESS)

        markdown = report_generator.build_markdown(self.db, ADDRESS)

        self.assertIn("## Social Media Footprint", markdown)
        self.assertIn("Also seen by", markdown)
        self.assertIn("maigret, sherlock", markdown)
        self.assertIn("CORROBORATED", markdown)
        self.assertEqual(markdown.count("| Facebook |"), 1)

    def test_an_uncorroborated_profile_stays_unverified(self):
        add_finding(self.db, "Sherlock", "https://github.com/vitalik")

        markdown = report_generator.build_markdown(self.db, ADDRESS)

        self.assertIn("| sherlock | UNVERIFIED |", markdown)

    def test_no_profiles_is_stated_explicitly(self):
        markdown = report_generator.build_markdown(self.db, ADDRESS)
        self.assertIn("_No social profiles recorded._", markdown)


if __name__ == "__main__":
    unittest.main()
