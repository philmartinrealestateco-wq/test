"""Entry point for the intel engine.

Opens ``intel.db`` (creating the schema on first run) and then hands off to the
on-chain enrichment pass.
"""

from __future__ import annotations

import sqlite3

from database import DB_PATH, init_db
from onchain_scraper import run as run_onchain


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

    return run_onchain()


if __name__ == "__main__":
    raise SystemExit(main())
