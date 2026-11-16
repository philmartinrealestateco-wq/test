"""Phase 2d: store a FlowLens case as per-transfer evidence.

Reads a case exported by the FlowLens CLI (a sibling checkout, not a Python
dependency), keeps only the transfers that touch the target wallet and replaces
that wallet's rows so a re-import does not duplicate them.

The provider coverage is stored on every row: a partial trace must never read
like a complete history.
"""

from __future__ import annotations

import argparse
import sys

from adapters.flowlens_adapter import FlowLensCase, FlowLensError, load_case
from database import DB_PATH, init_db
from onchain_scraper import utc_now

TOOL = "FlowLens"
CATEGORY = "transaction_trace"
CONFIDENCE = "VERIFIED"


def wallet_exists(db, address: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM wallets WHERE lower(address) = lower(?)", (address,)
    ).fetchone()
    return row is not None


def store_case(db, case: FlowLensCase) -> int:
    """Replace this wallet's flow rows with the case contents."""
    coverage = case.coverage_note()
    now = utc_now()
    db.execute("DELETE FROM flow_evidence WHERE lower(address) = lower(?)", (case.address,))
    db.executemany(
        """
        INSERT INTO flow_evidence
            (address, chain_id, tx_hash, event_index, block_number, block_time,
             direction, counterparty, amount, asset, kind, provider, source_url,
             coverage, confidence, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                case.address,
                transfer.chain_id,
                transfer.tx_hash,
                transfer.event_index,
                transfer.block_number,
                transfer.block_time,
                transfer.direction,
                transfer.counterparty,
                transfer.amount,
                transfer.asset,
                transfer.kind,
                transfer.provider,
                transfer.source_url,
                coverage,
                CONFIDENCE,
                now,
            )
            for transfer in case.transfers
        ],
    )
    db.commit()
    return len(case.transfers)


def run(case_path: str, address: str | None = None, dry_run: bool = False) -> int:
    db = init_db()
    try:
        try:
            case = load_case(case_path, address)
        except FlowLensError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

        if not wallet_exists(db, case.address):
            print(f"error: {case.address} is not cached in intel.db", file=sys.stderr)
            print("hint: run run_intel.py first so the wallet exists", file=sys.stderr)
            return 1

        print(f"address  : {case.address}")
        print(f"mode     : {case.mode} · updated {case.updated_at}")
        print(f"coverage : {case.coverage_note()}")
        if case.skipped:
            print(f"skipped  : {case.skipped} transfer(s) not touching this wallet")

        if not case.transfers:
            if case.partial:
                print(
                    "error: the provider reported partial coverage and returned no transfers; "
                    "treating this as an incomplete trace, not an empty wallet",
                    file=sys.stderr,
                )
                return 1
            print("transfers: 0 (the provider reports complete coverage for this range)")
        else:
            inflow = sum(1 for t in case.transfers if t.direction == "in")
            print(
                f"transfers: {len(case.transfers)} "
                f"({inflow} in, {len(case.transfers) - inflow} out)"
            )
            for transfer in case.transfers[:5]:
                print(
                    f"  {transfer.block_time} {transfer.direction:>3} "
                    f"{transfer.amount} {transfer.asset} "
                    f"{transfer.counterparty} {transfer.tx_hash}"
                )

        if dry_run:
            print("dry run: nothing written to the database")
            return 0

        stored = store_case(db, case)
        print(f"stored   : {stored} evidence row(s)")
        print(f"database : {DB_PATH}")
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", required=True, help="FlowLens case JSON export")
    parser.add_argument(
        "--address",
        help="wallet to attribute the transfers to (defaults to the case root)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args(argv)
    return run(args.case, address=args.address, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
