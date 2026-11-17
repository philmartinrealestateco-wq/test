"""Self-audit: prove control of a wallet, then check one email for breach exposure.

Ownership is proven with an EIP-191 ``personal_sign`` over a one-time challenge
that names the address, the email and a random nonce, so a signature from an
earlier run cannot be replayed. ``--bypass-signature`` exists for pipeline tests
only: it stores ``BYPASSED`` and the PDF prints a red warning.

The SQLite schema is untouched. Findings are ordinary ``osint_findings`` rows:
``BreachAPI`` / ``breach_exposure`` for the lookup and ``SelfAudit`` /
``ownership_verification`` for the proof.
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from datetime import datetime, timezone

from adapters import breach_adapter
from database import DB_PATH, init_db

AUTH_PHRASE = "I AM AUTHORIZED"
TOOL_BREACH = "BreachAPI"
TOOL_SELF = "SelfAudit"
CATEGORY_BREACH = "breach_exposure"
CATEGORY_OWNERSHIP = "ownership_verification"
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def normalize_address(value: str) -> str:
    address = (value or "").strip()
    if not _ADDRESS.match(address):
        raise ValueError("wallet address must be 0x followed by 40 hex characters")
    return address.lower()


def authorization_granted(answer: str | None) -> bool:
    """The prompt is a deliberate speed bump, not a click-through."""
    return " ".join((answer or "").split()).upper() == AUTH_PHRASE


def build_challenge(address: str, email: str, issued: str) -> str:
    """A human-signable statement that cannot be reused for another audit."""
    return "\n".join(
        [
            "intel_engine self-audit ownership proof",
            f"address: {address}",
            f"email: {email}",
            f"nonce: {secrets.token_hex(16)}",
            f"issued: {issued}",
            (
                "statement: I control this address and authorize a "
                "breach-exposure lookup for this email."
            ),
        ]
    )


def verify_signature(message: str, signature: str, address: str) -> str:
    """Recover the EIP-191 signer and require it to be the audited address."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError as error:
        raise ValueError("eth-account is not installed; cannot verify a signature") from error

    candidate = (signature or "").strip()
    if not candidate:
        raise ValueError("no signature was supplied")

    try:
        recovered = Account.recover_message(
            encode_defunct(text=message),
            signature=candidate,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"the signature could not be parsed: {error}") from error

    if recovered.lower() != address.lower():
        raise ValueError(f"the signature belongs to {recovered}, not {address}")
    return recovered


def ensure_wallet(db, address: str) -> bool:
    """Add the address so the report can load it; ENS fields stay empty."""
    existing = db.execute(
        "SELECT 1 FROM wallets WHERE lower(address) = lower(?)",
        (address,),
    ).fetchone()
    if existing is not None:
        return False

    db.execute(
        "INSERT INTO wallets (address, last_scanned) VALUES (?, NULL)",
        (address,),
    )
    db.commit()
    return True


def store_ownership(
    db,
    address: str,
    run_id: str,
    verification: dict,
    now: str,
) -> None:
    payload = {
        "version": 1,
        "run_id": run_id,
        "method": verification["method"],
        "challenge": verification.get("challenge"),
        "signature": verification.get("signature"),
        "recovered": verification.get("recovered"),
        "verified_at": now,
    }
    db.execute(
        """
        INSERT INTO osint_findings
            (address, tool, finding, url, confidence, timestamp, category)
        VALUES (?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            address,
            TOOL_SELF,
            breach_adapter.encode_finding(payload),
            verification["confidence"],
            now,
            CATEGORY_OWNERSHIP,
        ),
    )
    db.commit()


def store_breach(
    db,
    address: str,
    result: breach_adapter.BreachResult,
    run_id: str,
    now: str,
) -> int:
    records = breach_adapter.finding_records(result, run_id)
    for finding, url, confidence in records:
        db.execute(
            """
            INSERT INTO osint_findings
                (address, tool, finding, url, confidence, timestamp, category)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                address,
                TOOL_BREACH,
                finding,
                url,
                confidence,
                now,
                CATEGORY_BREACH,
            ),
        )
    db.commit()
    return len(records)


def store_failure(
    db,
    address: str,
    email: str,
    provider: str,
    detail: str,
    run_id: str,
    now: str,
) -> None:
    finding, url, confidence = breach_adapter.error_record(
        email,
        provider,
        detail,
        run_id,
    )
    db.execute(
        """
        INSERT INTO osint_findings
            (address, tool, finding, url, confidence, timestamp, category)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            address,
            TOOL_BREACH,
            finding,
            url,
            confidence,
            now,
            CATEGORY_BREACH,
        ),
    )
    db.commit()


def audit(
    db,
    address: str,
    email: str,
    verification: dict,
    *,
    lookup_fn=breach_adapter.lookup,
    api_key: str | None = None,
    provider: str | None = None,
    run_id: str | None = None,
    now: str | None = None,
) -> tuple[breach_adapter.BreachResult | None, breach_adapter.BreachError | None]:
    """Store the proof and exactly one lookup; API failures are returned, not raised."""
    now = now or utc_now()
    run_id = run_id or secrets.token_hex(8)

    ensure_wallet(db, address)
    store_ownership(db, address, run_id, verification, now)

    provider_label = "unknown"
    try:
        provider_name = provider or breach_adapter.provider_from_env()
        provider_label = breach_adapter.PROVIDERS.get(provider_name)
        if provider_label is None:
            raise breach_adapter.BreachConfigError(
                f"unknown provider {provider_name!r}; "
                f"supported: {', '.join(sorted(breach_adapter.PROVIDERS))}"
            )

        key = api_key if api_key is not None else breach_adapter.api_key_from_env()
        result = lookup_fn(email, key, provider=provider_name)
        store_breach(db, address, result, run_id, now)
    except breach_adapter.BreachError as error:
        store_failure(db, address, email, provider_label, str(error), run_id, now)
        return None, error

    return result, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--address",
        help="wallet to prove control of (prompted when omitted)",
    )
    parser.add_argument(
        "--email",
        help="email to check for breach exposure (prompted when omitted)",
    )
    parser.add_argument(
        "--bypass-signature",
        action="store_true",
        help="TEST ONLY: skip the signature check and store the run as BYPASSED",
    )
    args = parser.parse_args(argv)

    try:
        breach_adapter.provider_from_env()
        breach_adapter.api_key_from_env()
    except breach_adapter.BreachConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        address = normalize_address(
            args.address or input("Wallet address (0x...): ")
        )
    except EOFError:
        print("error: a wallet address is required", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        email = breach_adapter.normalize_email(
            args.email or input("Email to audit: ")
        )
    except EOFError:
        print("error: an email address is required", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print("\nThis tool asks a third-party breach API about an email address.")
    print("Run it only for data you own or are explicitly authorised to audit.")
    try:
        answer = input(f'Type "{AUTH_PHRASE}" to continue: ')
    except EOFError:
        print("\nerror: authorisation was not confirmed", file=sys.stderr)
        return 2
    if not authorization_granted(answer):
        print(
            "error: authorisation not confirmed; nothing was queried or stored",
            file=sys.stderr,
        )
        return 2

    challenge = build_challenge(address, email, utc_now())

    if args.bypass_signature:
        print("\n*** TEST MODE: --bypass-signature ***")
        print("Ownership is NOT verified; the report will mark this run BYPASSED.\n")
        verification = {
            "method": "bypass",
            "challenge": challenge,
            "signature": None,
            "recovered": None,
            "confidence": "BYPASSED",
        }
    else:
        print("\nSign this exact message with the wallet that controls", address)
        print("(EIP-191 personal_sign, e.g. Etherscan's Sign Message tool):\n")
        print(challenge)
        print()
        try:
            signature = input("Signature (0x...): ")
        except EOFError:
            signature = ""

        try:
            recovered = verify_signature(challenge, signature, address)
        except ValueError as error:
            print(f"error: ownership not proven: {error}", file=sys.stderr)
            print("Nothing was queried or stored.", file=sys.stderr)
            return 2

        verification = {
            "method": "eip191",
            "challenge": challenge,
            "signature": signature.strip(),
            "recovered": recovered,
            "confidence": "VERIFIED",
        }
        print(f"\nOwnership verified: signature recovered {recovered}")

    db = init_db()
    try:
        result, error = audit(db, address, email, verification)
    finally:
        db.close()

    if error is not None:
        masked = breach_adapter.mask_email(email)
        print(f"error: breach lookup failed: {error}", file=sys.stderr)
        print(
            f"stored as ERROR: exposure for {masked} is unknown, not absent",
            file=sys.stderr,
        )
        print(f"database: {DB_PATH}", file=sys.stderr)
        return 1

    if result is None:
        print("error: the lookup returned no result", file=sys.stderr)
        return 1

    masked = breach_adapter.mask_email(email)
    if result.status == "clean":
        print(f"breach result: no exposure returned for {masked} ({result.provider})")
    else:
        print(
            f"breach result: {len(result.breaches)} breach(es) for "
            f"{masked} ({result.provider})"
        )
        for breach in result.breaches:
            records = (
                f"{breach.pwn_count:,}"
                if breach.pwn_count is not None
                else "unknown"
            )
            print(
                f"  - {breach.name} "
                f"({breach.breach_date or 'date unknown'}): {records} record(s)"
            )

    print(f"database: {DB_PATH}")
    print(f"report  : report_generator.py --address {address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
