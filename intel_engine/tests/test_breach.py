"""Tests for the XposedOrNot adapter, self-audit storage and the PDF section."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import report_generator
import self_audit
from adapters import breach_adapter
from adapters.breach_adapter import (
    BreachAPIError,
    BreachConfigError,
    BreachResult,
)
from database import SCHEMA
from pypdf import PdfReader

ADDRESS = "0x18e296053cbdf986196903e889b7dca7a73882f6"
OTHER = "0xc78ddcdb9e90a02ec6ace446037c6079d773efd6"
EMAIL = "test@example.com"
NOW = "2026-11-17 12:00:00 UTC"


class FakeClient:
    """Mimics the raw XposedOrNot response shape, not just the dataclasses."""

    def __init__(
        self,
        names=None,
        details=None,
        email_error=None,
        analytics_error=None,
    ):
        self.names = list(names or [])
        self.details = list(details or [])
        self.email_error = email_error
        self.analytics_error = analytics_error
        self.email_calls = 0
        self.analytics_calls = 0

    def check_email(self, email):
        self.email_calls += 1
        if self.email_error is not None:
            raise self.email_error
        return {"breaches": list(self.names), "email": email, "status": "success"}

    def breach_analytics(self, email):
        self.analytics_calls += 1
        if self.analytics_error is not None:
            raise self.analytics_error
        return {"breaches_details": list(self.details)}


def detail(
    name,
    date="2013",
    data="Email addresses;Passwords",
    records=152445165,
    verified="Yes",
):
    return {
        "breach": name,
        "xposed_date": date,
        "xposed_data": data,
        "xposed_records": records,
        "verified": verified,
    }


def hit_client():
    return FakeClient(
        names=["Adobe", "LinkedIn"],
        details=[
            detail("Adobe", "2013", "Email addresses;Passwords", 152445165, "Yes"),
            detail("LinkedIn", "2012", "Email addresses", 164611595, "No"),
        ],
    )


def hit_result():
    return breach_adapter.check_email(EMAIL, client=hit_client())


def clean_result():
    return BreachResult(email=EMAIL, provider=breach_adapter.PROVIDER, status="clean")


def memory_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def verified() -> dict:
    return {
        "method": "eip191",
        "challenge": "intel_engine self-audit ownership proof",
        "signature": "0xdeadbeef",
        "recovered": ADDRESS,
        "confidence": "VERIFIED",
    }


def bypassed() -> dict:
    return {
        "method": "bypass",
        "challenge": "intel_engine self-audit ownership proof",
        "signature": None,
        "recovered": None,
        "confidence": "BYPASSED",
    }


def run_audit(db, verification, lookup_fn, run_id, now=NOW):
    return self_audit.audit(
        db,
        ADDRESS,
        EMAIL,
        verification,
        lookup_fn=lookup_fn,
        run_id=run_id,
        now=now,
    )


class LookupTests(unittest.TestCase):
    def test_a_hit_is_parsed_into_breaches(self):
        result = breach_adapter.check_email(EMAIL, client=hit_client())

        self.assertEqual(result.status, "hit")
        self.assertEqual(
            [breach.name for breach in result.breaches],
            ["Adobe", "LinkedIn"],
        )
        self.assertEqual(result.breaches[0].breach_date, "2013")
        self.assertEqual(result.breaches[0].pwn_count, 152445165)
        self.assertEqual(
            result.breaches[0].data_classes,
            ("Email addresses", "Passwords"),
        )
        self.assertEqual(
            result.breaches[0].url,
            "https://xposedornot.com/breach/Adobe",
        )
        self.assertTrue(result.breaches[0].is_verified)
        self.assertFalse(result.breaches[1].is_verified)

    def test_the_nested_free_api_shape_is_flattened(self):
        client = FakeClient(
            names=[["Adobe", "LinkedIn"]],
            details=[detail("Adobe"), detail("LinkedIn")],
        )
        result = breach_adapter.check_email(EMAIL, client=client)

        self.assertEqual(
            [breach.name for breach in result.breaches],
            ["Adobe", "LinkedIn"],
        )

    def test_a_clean_email_is_not_sent_to_analytics(self):
        client = FakeClient(names=[])
        result = breach_adapter.check_email(EMAIL, client=client)

        self.assertEqual(result.status, "clean")
        self.assertEqual(result.breaches, [])
        self.assertEqual(client.email_calls, 1)
        self.assertEqual(client.analytics_calls, 0)

    def test_a_check_failure_is_an_error(self):
        client = FakeClient(email_error=RuntimeError("network down"))
        with self.assertRaises(BreachAPIError):
            breach_adapter.check_email(EMAIL, client=client)

    def test_an_analytics_failure_is_an_error(self):
        client = FakeClient(
            names=["Adobe"],
            analytics_error=AttributeError("null ExposedBreaches"),
        )
        with self.assertRaises(BreachAPIError):
            breach_adapter.check_email(EMAIL, client=client)

    def test_names_without_details_are_kept_as_unverified(self):
        client = FakeClient(
            names=["Adobe", "Mystery"],
            details=[detail("Adobe")],
        )
        result = breach_adapter.check_email(EMAIL, client=client)

        mystery = result.breaches[1]
        self.assertEqual(mystery.name, "Mystery")
        self.assertEqual(mystery.breach_date, "")
        self.assertEqual(mystery.data_classes, ())
        self.assertIsNone(mystery.pwn_count)
        self.assertFalse(mystery.is_verified)
        self.assertEqual(
            mystery.url,
            "https://xposedornot.com/breach/Mystery",
        )

    def test_missing_package_is_a_config_error(self):
        with mock.patch.dict(sys.modules, {"xposedornot": None}):
            with self.assertRaises(BreachConfigError):
                breach_adapter.ensure_available()

    def test_email_is_normalised_and_masked(self):
        self.assertEqual(breach_adapter.normalize_email(" Test@Example.COM "), EMAIL)
        self.assertEqual(breach_adapter.mask_email(EMAIL), "t***@example.com")
        self.assertEqual(breach_adapter.mask_email(None), "***")

    def test_classes_are_split_and_deduplicated(self):
        classes = breach_adapter._split_classes(
            "Email addresses; Passwords;Email addresses"
        )
        self.assertEqual(classes, ("Email addresses", "Passwords"))

    def test_verified_yes_and_no_are_parsed(self):
        self.assertTrue(breach_adapter._is_verified("Yes"))
        self.assertFalse(breach_adapter._is_verified("No"))
        self.assertTrue(breach_adapter._is_verified(True))
        self.assertFalse(breach_adapter._is_verified(False))

    def test_finding_records_use_the_xposedornot_provider(self):
        rows = breach_adapter.finding_records(hit_result(), "run1")
        payload = breach_adapter.decode_finding(rows[0][0])

        self.assertEqual(payload["provider"], "XposedOrNot")
        self.assertEqual(payload["name"], "Adobe")
        self.assertEqual(payload["date"], "2013")
        self.assertEqual(payload["classes"], ["Email addresses", "Passwords"])
        self.assertEqual(rows[0][2], "VERIFIED")
        self.assertEqual(rows[1][2], "UNVERIFIED")


class OwnershipTests(unittest.TestCase):
    def test_the_exact_phrase_is_required(self):
        self.assertTrue(self_audit.authorization_granted("I AM AUTHORIZED"))
        self.assertTrue(self_audit.authorization_granted("  i am   authorized "))
        self.assertFalse(self_audit.authorization_granted("yes"))
        self.assertFalse(self_audit.authorization_granted(""))

    def test_a_challenge_names_the_address_email_and_a_fresh_nonce(self):
        first = self_audit.build_challenge(ADDRESS, EMAIL, NOW)
        second = self_audit.build_challenge(ADDRESS, EMAIL, NOW)

        self.assertIn(ADDRESS, first)
        self.assertIn(EMAIL, first)
        self.assertNotEqual(first, second)

    def test_a_real_signature_round_trips(self):
        from eth_account import Account
        from eth_account.messages import encode_defunct

        account = Account.create()
        challenge = self_audit.build_challenge(account.address.lower(), EMAIL, NOW)
        signature = Account.sign_message(
            encode_defunct(text=challenge),
            account.key,
        ).signature.hex()

        recovered = self_audit.verify_signature(challenge, signature, account.address)
        self.assertEqual(recovered.lower(), account.address.lower())

        with self.assertRaises(ValueError):
            self_audit.verify_signature(challenge, signature, OTHER)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)

    def test_a_hit_is_stored_under_breachapi_breach_exposure(self):
        result, error = run_audit(
            self.db,
            verified(),
            lambda email: hit_result(),
            "run1",
        )

        self.assertIsNone(error)
        self.assertEqual(result.status, "hit")

        row = self.db.execute(
            "SELECT tool, category, confidence, url, finding FROM osint_findings"
            " WHERE tool = 'BreachAPI'"
        ).fetchone()
        payload = breach_adapter.decode_finding(row["finding"])

        self.assertEqual(row["category"], "breach_exposure")
        self.assertEqual(row["confidence"], "VERIFIED")
        self.assertEqual(
            row["url"],
            "https://xposedornot.com/breach/Adobe",
        )
        self.assertEqual(payload["provider"], "XposedOrNot")
        self.assertEqual(payload["name"], "Adobe")
        self.assertEqual(payload["date"], "2013")
        self.assertEqual(payload["email"], EMAIL)

    def test_a_clean_answer_is_stored_as_clean(self):
        run_audit(
            self.db,
            verified(),
            lambda email: clean_result(),
            "run2",
        )

        row = self.db.execute(
            "SELECT confidence FROM osint_findings WHERE tool = 'BreachAPI'"
        ).fetchone()
        self.assertEqual(row["confidence"], "CLEAN")

    def test_a_failed_lookup_is_stored_as_error(self):
        def fail(email):
            raise BreachAPIError("XposedOrNot request failed")

        result, error = run_audit(self.db, verified(), fail, "run3")

        self.assertIsNone(result)
        self.assertIsNotNone(error)

        row = self.db.execute(
            "SELECT confidence, finding FROM osint_findings WHERE tool = 'BreachAPI'"
        ).fetchone()
        self.assertEqual(row["confidence"], "ERROR")
        self.assertEqual(
            breach_adapter.decode_finding(row["finding"])["status"],
            "error",
        )

    def test_a_bypassed_run_is_stored_as_bypassed(self):
        run_audit(
            self.db,
            bypassed(),
            lambda email: hit_result(),
            "run4",
        )

        row = self.db.execute(
            "SELECT confidence FROM osint_findings WHERE tool = 'SelfAudit'"
        ).fetchone()
        self.assertEqual(row["confidence"], "BYPASSED")

    def test_an_audited_wallet_is_added_to_the_wallet_table(self):
        run_audit(
            self.db,
            verified(),
            lambda email: hit_result(),
            "run5",
        )

        row = self.db.execute(
            "SELECT last_scanned FROM wallets WHERE lower(address) = lower(?)",
            (ADDRESS,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["last_scanned"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def audit_hit(self, verification=None):
        return run_audit(
            self.db,
            verification or verified(),
            lambda email: hit_result(),
            "run-report",
        )

    def test_the_section_names_the_breaches_and_the_ownership_limit(self):
        self.audit_hit()
        markdown = report_generator.build_markdown(self.db, ADDRESS)

        self.assertIn("## Breach Exposure & Ownership Verification", markdown)
        self.assertIn("Breach exposure is not proof of wallet ownership", markdown)
        self.assertIn("XposedOrNot", markdown)
        self.assertIn("Adobe", markdown)
        self.assertIn("2013", markdown)
        self.assertIn("LinkedIn", markdown)
        self.assertIn("2012", markdown)
        self.assertIn("t***@example.com", markdown)
        self.assertNotIn(EMAIL, markdown)
        self.assertIn("Ownership verified", markdown)

    def test_a_bypassed_run_is_an_alert(self):
        self.audit_hit(bypassed())
        markdown = report_generator.build_markdown(self.db, ADDRESS)

        self.assertIn("Ownership NOT verified", markdown)
        self.assertIn("--bypass-signature", markdown)

    def test_a_clean_answer_is_not_rendered_as_no_lookup(self):
        run_audit(
            self.db,
            verified(),
            lambda email: clean_result(),
            "run-clean",
        )

        markdown = report_generator.build_markdown(self.db, ADDRESS)
        self.assertIn("returned no breach", markdown)
        self.assertNotIn("_No breach lookup has been recorded", markdown)

    def test_a_failed_lookup_says_unknown_not_absent(self):
        def fail(email):
            raise BreachAPIError("XposedOrNot request failed")

        run_audit(self.db, verified(), fail, "run-error")

        markdown = report_generator.build_markdown(self.db, ADDRESS)
        self.assertIn("Breach lookup failed", markdown)
        self.assertIn("unknown, not absent", markdown)

    def test_the_newest_run_wins(self):
        run_audit(
            self.db,
            verified(),
            lambda email: clean_result(),
            "run-old",
            now="2026-11-17 11:00:00 UTC",
        )
        self.audit_hit()

        markdown = report_generator.build_markdown(self.db, ADDRESS)
        self.assertIn("Adobe", markdown)
        self.assertNotIn("returned no breach", markdown)

    def test_the_section_survives_pdf_rendering(self):
        self.audit_hit()
        markdown = report_generator.build_markdown(self.db, ADDRESS)
        pdf = report_generator.render_pdf(
            markdown,
            Path(self.tmp.name) / "audit.pdf",
        )

        text = " ".join(page.extract_text() for page in PdfReader(str(pdf)).pages)
        text = " ".join(text.split())

        self.assertIn("Breach Exposure & Ownership Verification", text)
        self.assertIn("Breach exposure is not proof of wallet ownership", text)
        self.assertIn("XposedOrNot", text)
        self.assertIn("Adobe", text)
        self.assertIn("2013", text)


if __name__ == "__main__":
    unittest.main()
