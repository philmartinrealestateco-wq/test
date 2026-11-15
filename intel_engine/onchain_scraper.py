"""Phase 2: on-chain enrichment of the master target list.

Reads the highest-value wallets from ``out/master_targets.csv``, reverse-resolves
ENS over a public Ethereum RPC, keeps the text records worth pivoting on, and
runs one DuckDuckGo search per address. Everything lands in ``intel.db`` so a
rerun skips wallets that already completed.

A failed lookup is never recorded as an absence: an RPC or search error leaves
``wallets.last_scanned`` NULL so the next run retries that wallet.
"""

from __future__ import annotations

import argparse
import csv
import re
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from duckduckgo_search import DDGS
from ens.ens import ENS
from web3 import Web3

from database import DB_PATH, init_db

# duckduckgo-search announces its rename to `ddgs` on every call; this phase is
# pinned to the package the brief names, so keep the per-wallet output readable.
warnings.filterwarnings("ignore", message=r".*renamed to `ddgs`.*")

ROOT = Path(__file__).resolve().parent.parent
MASTER_CSV = ROOT / "out" / "master_targets.csv"

DEFAULT_LIMIT = 5
SEARCH_RESULTS = 3
REQUEST_DELAY = 2.0

RPC_ENDPOINTS = (
    "https://ethereum-rpc.publicnode.com",
    "https://eth.drpc.org",
    "https://1rpc.io/eth",
)

TEXT_KEYS = ("com.twitter", "email", "url")

_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class Target:
    address: str
    best_usd: str
    networks: str
    status: str


@dataclass(frozen=True)
class EnsRecord:
    name: str
    twitter: str
    email: str
    url: str


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    mentions_address: bool


def read_targets(path: Path = MASTER_CSV, limit: int = DEFAULT_LIMIT) -> list[Target]:
    """Return the first ``limit`` EVM wallets from the master CSV."""
    if not path.exists():
        raise SystemExit(f"error: {path} not found; run scripts\\run_pipeline.py first")

    targets: list[Target] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            address = (row.get("Wallet") or "").strip()
            if not _EVM_ADDRESS.match(address):
                continue
            targets.append(
                Target(
                    address=Web3.to_checksum_address(address),
                    best_usd=(row.get("Best Total USD") or "").strip(),
                    networks=(row.get("Networks") or "").strip(),
                    status=(row.get("Status") or "").strip(),
                )
            )
            if len(targets) >= limit:
                break

    return targets


def connect_rpc(endpoints: tuple[str, ...] = RPC_ENDPOINTS) -> tuple[Web3, str]:
    """Return the first endpoint that answers eth_chainId."""
    failures: list[str] = []
    for endpoint in endpoints:
        web3 = Web3(Web3.HTTPProvider(endpoint, request_kwargs={"timeout": 20}))
        try:
            chain_id = web3.eth.chain_id
        except Exception as error:  # noqa: BLE001 - any transport shape means "try the next"
            failures.append(f"{endpoint}: {type(error).__name__}")
            continue

        if chain_id == 1:
            return web3, endpoint

        failures.append(f"{endpoint}: chain id {chain_id}")

    raise RuntimeError("no public Ethereum RPC answered (" + "; ".join(failures) + ")")


def enrich_ens(ens: ENS, address: str) -> EnsRecord | None:
    """Return the ENS record for ``address``, or None when it has no reverse record."""
    name = ens.name(address)
    if not name:
        return None

    records = {key: ens.get_text(name, key) for key in TEXT_KEYS}
    return EnsRecord(
        name=name,
        twitter=records["com.twitter"],
        email=records["email"],
        url=records["url"],
    )


def search_address(address: str, results: int = SEARCH_RESULTS) -> list[SearchHit]:
    """Run the DuckDuckGo pivot for a raw address."""
    items = DDGS().text(f"{address} wallet", max_results=results) or []
    hits: list[SearchHit] = []
    for item in items:
        title = (item.get("title") or "").strip()
        url = (item.get("href") or item.get("url") or "").strip()
        haystack = f"{title} {item.get('body') or ''}".lower()
        hits.append(
            SearchHit(
                title=title,
                url=url,
                mentions_address=address.lower() in haystack,
            )
        )

    return hits


def already_scanned(db, address: str) -> bool:
    row = db.execute(
        "SELECT last_scanned FROM wallets WHERE address = ?", (address,)
    ).fetchone()
    return bool(row and row["last_scanned"])


def store_wallet(db, address: str, record: EnsRecord | None, complete: bool) -> None:
    """Upsert the ENS cache; COALESCE keeps values a previous run already found."""
    scanned_at = utc_now() if complete else None
    name = record.name if record else None
    twitter = (record.twitter or None) if record else None
    email = (record.email or None) if record else None
    db.execute(
        """
        INSERT INTO wallets (address, ens_name, ens_twitter, ens_email, last_scanned)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(address) DO UPDATE SET
            ens_name = COALESCE(excluded.ens_name, wallets.ens_name),
            ens_twitter = COALESCE(excluded.ens_twitter, wallets.ens_twitter),
            ens_email = COALESCE(excluded.ens_email, wallets.ens_email),
            last_scanned = COALESCE(excluded.last_scanned, wallets.last_scanned)
        """,
        (address, name, twitter, email, scanned_at),
    )


def store_findings(db, address: str, tool: str, rows: list[tuple[str, str]], confidence: str) -> None:
    """Replace this address's rows for ``tool`` so reruns do not duplicate."""
    db.execute(
        "DELETE FROM osint_findings WHERE address = ? AND tool = ?", (address, tool)
    )
    if not rows:
        return

    now = utc_now()
    db.executemany(
        """
        INSERT INTO osint_findings (address, tool, finding, url, confidence, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(address, tool, finding, url, confidence, now) for finding, url in rows],
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def run(limit: int = DEFAULT_LIMIT, master: Path = MASTER_CSV) -> int:
    targets = read_targets(master, limit)
    if not targets:
        print(f"error: no EVM wallets in {master}")
        return 1

    print(f"master : {master}")
    print(f"targets: {len(targets)} highest-value wallet(s)")

    try:
        web3, endpoint = connect_rpc()
    except RuntimeError as error:
        print(f"error: {error}")
        return 1

    print(f"rpc    : {endpoint}\n")
    ens = ENS.from_web3(web3)
    db = init_db()

    found_ens = 0
    total_hits = 0
    corroborated = 0
    skipped = 0
    errors = 0

    try:
        for index, target in enumerate(targets, start=1):
            print(f"[{index}/{len(targets)}] {target.address}")
            print(
                f"      ${target.best_usd} · {target.networks or 'unknown'} · "
                f"{target.status or 'unknown'}"
            )

            if already_scanned(db, target.address):
                print("      skipped  : already in intel.db\n")
                skipped += 1
                continue

            failures: list[str] = []

            record: EnsRecord | None = None
            try:
                record = enrich_ens(ens, target.address)
            except Exception as error:  # noqa: BLE001 - report, never treat as "no name"
                failures.append(f"ENS {type(error).__name__}: {str(error)[:90]}")
                print(f"      ENS      : ERROR ({type(error).__name__}: {str(error)[:90]})")
            else:
                if record is None:
                    print("      ENS      : no reverse record")
                else:
                    found_ens += 1
                    print(f"      ENS      : {record.name}")
                    for label, value in (
                        ("twitter", record.twitter),
                        ("email", record.email),
                        ("url", record.url),
                    ):
                        if value:
                            print(f"                 {label}: {value}")

            hits: list[SearchHit] = []
            try:
                hits = search_address(target.address)
            except Exception as error:  # noqa: BLE001 - same rule as ENS
                failures.append(f"search {type(error).__name__}: {str(error)[:90]}")
                print(f"      DDG      : ERROR ({type(error).__name__}: {str(error)[:90]})")
            else:
                total_hits += len(hits)
                mentioned = sum(1 for hit in hits if hit.mentions_address)
                corroborated += mentioned
                print(f"      DDG      : {len(hits)} result(s), {mentioned} mention the address")
                for hit in hits:
                    flag = "*" if hit.mentions_address else "-"
                    print(f"        {flag} {hit.title}")
                    print(f"          {hit.url}")

            if record and record.url:
                store_findings(db, target.address, "ENS", [("ENS url record", record.url)], "VERIFIED")
            if hits:
                store_findings(
                    db,
                    target.address,
                    "DuckDuckGo",
                    [(hit.title, hit.url) for hit in hits],
                    "UNVERIFIED",
                )

            store_wallet(db, target.address, record, complete=not failures)
            db.commit()

            if failures:
                errors += 1
                print("      cached   : partial — last_scanned stays NULL, will retry\n")
            else:
                print("      cached   : yes\n")

            time.sleep(REQUEST_DELAY)
    finally:
        db.close()

    print(
        f"summary: {len(targets)} target(s) · ENS names {found_ens} · "
        f"search results {total_hits} ({corroborated} mention the address) · "
        f"skipped {skipped} · errors {errors}"
    )
    print(f"database: {DB_PATH}")
    return 0 if not errors else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"how many wallets to process (default {DEFAULT_LIMIT})",
    )
    args = parser.parse_args(argv)
    return run(limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
