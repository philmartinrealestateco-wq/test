"""Entry point for the intel engine.

This phase only proves the storage layer: open ``intel.db``, create the schema on
first run, and report success. OSINT collection comes later.
"""

from __future__ import annotations

import sqlite3

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
        print(f"  file: {DB_PATH}")
    finally:
        connection.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
