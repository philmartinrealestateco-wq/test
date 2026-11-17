"""Tests for the breach adapter, self-audit storage and the PDF section."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import report_generator
import self_audit
from adapters import breach_adapter
from adapters.breach_adapter import (
    Breach,
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


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = (
            text
            if text is not None
            else ("" if payload is None else json.dumps(payload))
        )

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def hibp_payload() -> list[dict]:
    return [
        {
            "Name": "Adobe",
            "Title": "Adobe",
            "BreachDate": "2013-10-04",
            "PwnCount": 152445165,
            "DataClasses": ["Email addresses", "Passwords"],
            "IsVerified": True,
        },
        {
            "Name": "LinkedIn",
            "Title": "LinkedIn",
            "BreachDate": "2012-05-05",
            "PwnCount": 164611595,
            "DataClasses": ["Email addresses"],
            "IsVerified": False,
        },
    ]


def hit_result(email: str = EMAIL) -> BreachResult:
    return BreachResult(
        email=email,
        provider="HIBP",
        status="hit",
        breaches=[
            Breach(
                name="Adobe",
                title="Adobe",
                breach_date="2013-10-04",
                pwn_count=152445165,
                data_classes=("Email addresses", "Passwords"),
                is_verified=True,
                url="https://haveibeenpwned.com/PwnedWebsites#Adobe",
            )
        ],
    )


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
        api_key="test-key",
        provider="hibp",
        run_id=run_id,
        now=now,
    )


class LookupTests(unittest.TestCase):
    def test_a_hit_is_parsed_into_breaches(self):
        session = FakeSession(FakeResponse(200, hibp_payload()))
        result = breach_adapter.lookup(EMAIL, "key", session=session)

        self.assertEqual(result.status, "hit")
        self.assertEqual([breach.name for breach in result.breaches], ["Adobe", "LinkedIn"])
        self.assertEqual(result.breaches[0].breach_date, "2013-10-04")
        self.assertEqual(result.breaches[0].pwn_count, 152445165)
        self.assertEqual(
            result.breaches[0].data_classes,
            ("Email addresses", "Passwords"),
        )
        self.assertEqual(
            result.breaches[0].url,
            "https://haveibeenpwned.com/PwnedWebsites#Adobe",
        )

    def test_the_request_carries_the_key_and_a_user_agent(self):
        session = FakeSession(FakeResponse(200, hibp_payload()))
        breach_adapter.lookup("Test@Example.com", "secret", session=session)

        url, kwargs = session.calls[0]
        self.assertIn("test%40example.com", url)
        self.assertEqual(kwargs["headers"]["hibp-api-key"], "secret")
        self.assertIn("user-agent", kwargs["headers"])
        self.assertEqual(kwargs["params"], {"truncateResponse": "false"})

    def test_404_is_a_clean_answer_not_an_error(self):
        result = breach_adapter.lookup(
            EMAIL,
            "key",
            session=FakeSession(FakeResponse(404)),
        )
        self.assertEqual(result.status, "clean")
        self.assertEqual(result.breaches, [])

    def test_401_is_an_error(self):
        with self.assertRaises(BreachAPIError) as caught:
            breach_adapter.lookup(
                EMAIL,
                "key",
                session=FakeSession(FakeResponse(401)),
            )
        self.assertEqual(caught.exception.status, 401)

    def test_429_keeps_the_retry_hint(self):
        response = FakeResponse(429, headers={"Retry-After": "3"})
        with self.assertRaises(BreachAPIError) as caught:
            breach_adapter.lookup(EMAIL, "key", session=FakeSession(response))

        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, "3")
        self.assertIn("retry after 3s", str(caught.exception))

    def test_a_hit_with_no_parseable_breach_is_an_error(self):
        with self.assertRaises(BreachAPIError):
            breach_adapter.lookup(
                EMAIL,
                "key",
                session=FakeSession(FakeResponse(200, [])),
            )

    def test_missing_key_is_a_config_error(self):
        with self.assertRaises(BreachConfigError):
            breach_adapter.api_key_from_env({})

    def test_email_is_normalised_and_masked(self):
        self.assertEqual(breach_adapter.normalize_email(" Test@Example.COM "), EMAIL)
        self.assertEqual(breach_adapter.mask_email(EMAIL), "t***@example.com")
        self.assertEqual(breach_adapter.mask_email(None), "***")

    def test_unverified_breaches_are_stored_as_unverified(self):
        unverified = BreachResult(
            email=EMAIL,
            provider="HIBP",
            status="hit",
            breaches=[
                Breach("X", "X", "", None, (), False, "https://example.test/x")
            ],
        )
        self.assertEqual(
            breach_adapter.finding_records(unverified, "run1")[0][2],
            "UNVERIFIED",
        )


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
            lambda email, key, provider: hit_result(),
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
            "https://haveibeenpwned.com/PwnedWebsites#Adobe",
        )
        self.assertEqual(payload["name"], "Adobe")
        self.assertEqual(payload["date"], "2013-10-04")
        self.assertEqual(payload["email"], EMAIL)

    def test_a_clean_answer_is_stored_as_clean(self):
        clean = BreachResult(email=EMAIL, provider="HIBP", status="clean")
        run_audit(self.db, verified(), lambda email, key, provider: clean, "run2")

        row = self.db.execute(
            "SELECT confidence FROM osint_findings WHERE tool = 'BreachAPI'"
        ).fetchone()
        self.assertEqual(row["confidence"], "CLEAN")

    def test_a_failed_lookup_is_stored_as_error(self):
        def fail(email, key, provider):
            raise BreachAPIError("HTTP 429: rate limited", status=429)

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
            lambda email, key, provider: hit_result(),
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
            lambda email, key, provider: hit_result(),
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
            lambda email, key, provider: hit_result(),
            "run-report",
        )

    def test_the_section_names_the_breaches_and_the_ownership_limit(self):
        self.audit_hit()
        markdown = report_generator.build_markdown(self.db, ADDRESS)

        self.assertIn("## Breach Exposure & Ownership Verification", markdown)
        self.assertIn("Breach exposure is not proof of wallet ownership", markdown)
        self.assertIn("Adobe", markdown)
        self.assertIn("2013-10-04", markdown)
        self.assertIn("t***@example.com", markdown)
        self.assertNotIn(EMAIL, markdown)
        self.assertIn("Ownership verified", markdown)

    def test_a_bypassed_run_is_an_alert(self):
        self.audit_hit(bypassed())
        markdown = report_generator.build_markdown(self.db, ADDRESS)

        self.assertIn("Ownership NOT verified", markdown)
        self.assertIn("--bypass-signature", markdown)

    def test_a_clean_answer_is_not_rendered_as_no_lookup(self):
        clean = BreachResult(email=EMAIL, provider="HIBP", status="clean")
        run_audit(
            self.db,
            verified(),
            lambda email, key, provider: clean,
            "run-clean",
        )

        markdown = report_generator.build_markdown(self.db, ADDRESS)
        self.assertIn("returned no breach", markdown)
        self.assertNotIn("_No breach lookup has been recorded", markdown)

    def test_a_failed_lookup_says_unknown_not_absent(self):
        def fail(email, key, provider):
            raise BreachAPIError(
                "HTTP 401: the API rejected BREACH_API_KEY",
                status=401,
            )

        run_audit(self.db, verified(), fail, "run-error")

        markdown = report_generator.build_markdown(self.db, ADDRESS)
        self.assertIn("Breach lookup failed", markdown)
        self.assertIn("unknown, not absent", markdown)

    def test_the_newest_run_wins(self):
        clean = BreachResult(email=EMAIL, provider="HIBP", status="clean")
        run_audit(
            self.db,
            verified(),
            lambda email, key, provider: clean,
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
        self.assertIn("Adobe", text)
        self.assertIn("2013-10-04", text)


if __name__ == "__main__":
    unittest.main()
