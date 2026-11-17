"""Entry point for the intel engine.

Runs the whole chain over ``out/master_targets.csv``: on-chain enrichment, then
OpenSanctions screening of the cached wallets, then off-chain OSINT for whatever
pivots that produced, then a PDF for every wallet that ended up with at least
one finding.
"""

from __future__ import annotations

import sqlite3

import onchain_scraper
import osint_runner
import report_generator
import sanctions
from database import DB_PATH, init_db


def main() -> int:
    try:
        connection = init_db()
    except sqlite3.Error as error:
        print(f"Database connection failed: {error}")
        return 1

    try:
        connection.execute("SELECT 1").fetchone()
        print("Database connected successfully")
        print(f"  file: {DB_PATH}\n")
    finally:
        connection.close()

    print("== phase 2: on-chain enrichment ==")
    scrape_status = onchain_scraper.run(limit=None, web="pivots")
    print()

    print("== phase 2b: sanctions screening ==")
    sanctions_status = sanctions.run()
    print()

    print("== phase 3: off-chain OSINT ==")
    pivot_status = osint_runner.run()
    print()

    print("== phase 4: reports ==")
    db = init_db()
    try:
        with_findings = len(report_generator.wallets_with_findings(db))
        total = db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
        generated, skipped = report_generator.run_batch(db)
    finally:
        db.close()

    print()
    print(
        f"summary: {total} wallet(s) cached · {with_findings} with findings · "
        f"{len(generated)} pdf generated · {skipped} skipped (no findings)"
    )
    print(f"database: {DB_PATH}")
    print(f"reports : {report_generator.REPORTS_DIR}")

    return 0 if not (scrape_status or sanctions_status or pivot_status) else 1


if __name__ == "__main__":
    raise SystemExit(main())
