"""SQLite storage for the intel engine.

``wallets`` caches what ENS told us about an address; ``osint_findings`` keeps one
row per tool result, so a re-scan appends instead of overwriting history.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "intel.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    ens_name TEXT,
    ens_twitter TEXT,
    ens_email TEXT,
    last_scanned TIMESTAMP
);

CREATE TABLE IF NOT EXISTS osint_findings (
    address TEXT NOT NULL,
    tool TEXT NOT NULL,
    finding TEXT,
    url TEXT,
    confidence TEXT,
    timestamp TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_osint_findings_address
    ON osint_findings (address);
"""


def connect(path: str | Path = DB_PATH) -> sqlite3.Connection:
    """Open the database, creating the file on first use."""
    connection = sqlite3.connect(Path(path))
    connection.row_factory = sqlite3.Row
    return connection


def init_db(path: str | Path = DB_PATH) -> sqlite3.Connection:
    """Create any missing tables and return the open connection."""
    connection = connect(path)
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


if __name__ == "__main__":
    db = init_db()
    try:
        tables = [
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                " ORDER BY name"
            )
        ]
    finally:
        db.close()

    print(f"database ready: {DB_PATH}")
    print(f"tables: {', '.join(tables)}")
