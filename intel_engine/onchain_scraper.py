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
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from duckduckgo_search import DDGS
from ens.ens import ENS
from web3 import Web3

from database import DB_PATH, init_db

ROOT = Path(__file__).resolve().parent.parent
MASTER_CSV = ROOT / "out" / "master_targets.csv"

DEFAULT_LIMIT = 5
DEFAULT_DELAY = (10.0, 20.0)
DEFAULT_NO_WEB_DELAY = (1.0, 2.0)
DDG_FAILURE_LIMIT = 5
SEARCH_RESULTS = 3
SEARCH_DELAY = 1.0
WEB_MODES = ("all", "pivots", "none")
#: Keyword anchors, not operators: this DDG backend ignores both quotes and
#: ``site:`` (measured 2026-11-15 - quoted and site-scoped queries returned 0
#: results or unrelated filler), while "<address> etherscan" surfaced the real
#: explorer page. Relevance is enforced client-side instead.
SEARCH_QUERIES = (
    "{address} etherscan",
    "{address} holder",
    "{address} wallet",
)

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


def should_search_address(web: str, record: EnsRecord | None) -> bool:
    """Whether to run the address pivot for one wallet.

    ``pivots`` keeps DuckDuckGo for wallets with an ENS name and skips the
    address-only queries, which mostly return explorer filler for wallets that
    have no off-chain identity.
    """

    if web == "all":
        return True
    if web == "pivots":
        return record is not None
    if web == "none":
        return False
    raise ValueError(f"unknown web mode {web!r}; expected one of {', '.join(WEB_MODES)}")


def read_targets(path: Path = MASTER_CSV, limit: int | None = DEFAULT_LIMIT) -> list[Target]:
    """Return the first ``limit`` EVM wallets from the master CSV, or all of them."""
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
            if limit is not None and len(targets) >= limit:
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
    """Run the DuckDuckGo pivot, keeping only hits that name the address.

    A bare ``"<address> wallet"`` query returns unrelated filler when nothing
    matches, so each candidate is checked against the address and only
    explorer-scoped queries are added on top of the exact-phrase search.
    """
    hits: dict[str, SearchHit] = {}
    failures: list[str] = []

    for template in SEARCH_QUERIES:
        query = template.format(address=address)
        try:
            # duckduckgo-search prints a rename notice and installs its own
            # 'always' filter during the call, so it cannot be silenced from
            # here; it is stderr noise only.
            items = DDGS().text(query, max_results=results) or []
        except Exception as error:  # noqa: BLE001 - one query may be throttled
            failures.append(f"{query}: {type(error).__name__}")
            continue

        for item in items:
            title = (item.get("title") or "").strip()
            url = (item.get("href") or item.get("url") or "").strip()
            snippet = item.get("body") or ""
            if not url or not _names_address(address, title, snippet, url):
                continue
            hits.setdefault(url, SearchHit(title=title, url=url))

        time.sleep(SEARCH_DELAY)

    if failures and len(failures) == len(SEARCH_QUERIES):
        raise RuntimeError("; ".join(failures))

    return list(hits.values())[:results]


def _names_address(address: str, *parts: str) -> bool:
    """True when one of ``parts`` carries the full address.

    The URL counts: an explorer or forum page whose path *is* the address is a
    genuine hit even when the title abbreviates it.
    """
    needle = address.lower()
    return any(needle in part.lower() for part in parts)


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


def store_findings(
    db,
    address: str,
    tool: str,
    rows: list[tuple[str, str | None]],
    confidence: str,
) -> None:
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


def run(
    limit: int | None = DEFAULT_LIMIT,
    master: Path = MASTER_CSV,
    delay: tuple[float, float] | None = DEFAULT_DELAY,
    web: str = "all",
    no_web_delay: tuple[float, float] | None = DEFAULT_NO_WEB_DELAY,
) -> int:
    if web not in WEB_MODES:
        print(f"error: unknown web mode {web!r}; expected {', '.join(WEB_MODES)}")
        return 2

    targets = read_targets(master, limit)
    if not targets:
        print(f"error: no EVM wallets in {master}")
        return 1

    print(f"master : {master}")
    print(f"targets: {len(targets)} wallet(s)")
    print(f"web    : {web}")
    if delay:
        if no_web_delay:
            print(
                f"spacing: {delay[0]:.0f}-{delay[1]:.0f}s after web wallets, "
                f"{no_web_delay[0]:.0f}-{no_web_delay[1]:.0f}s after no-web wallets"
            )
        else:
            print(f"spacing: {delay[0]:.0f}-{delay[1]:.0f}s random between wallets")

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
    skipped = 0
    errors = 0
    search_failures_in_a_row = 0
    stopped_early = False

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
            search_enabled = should_search_address(web, record)
            if not search_enabled:
                print("      DDG      : skipped (no ENS pivot)")
            else:
                try:
                    hits = search_address(target.address)
                except Exception as error:  # noqa: BLE001 - same rule as ENS
                    failures.append(f"search {type(error).__name__}: {str(error)[:90]}")
                    print(f"      DDG      : ERROR ({type(error).__name__}: {str(error)[:90]})")
                else:
                    total_hits += len(hits)
                    print(f"      DDG      : {len(hits)} corroborated result(s)")
                    for hit in hits:
                        print(f"        * {hit.title}")
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

            search_failed = search_enabled and any(
                failure.startswith("search ") for failure in failures
            )
            if search_enabled:
                search_failures_in_a_row = (
                    search_failures_in_a_row + 1 if search_failed else 0
                )

            if failures:
                errors += 1
                print("      cached   : partial — last_scanned stays NULL, will retry\n")
            else:
                print("      cached   : yes\n")

            if search_failures_in_a_row >= DDG_FAILURE_LIMIT:
                stopped_early = True
                print(
                    f"stopping: DuckDuckGo failed for {search_failures_in_a_row} wallets in a row; "
                    "the remaining wallets were not scanned\n"
                )
                break

            sleep_delay = delay if search_enabled else no_web_delay
            if sleep_delay and index < len(targets):
                time.sleep(random.uniform(*sleep_delay))
    finally:
        db.close()

    print(
        f"summary: {len(targets)} target(s) · ENS names {found_ens} · "
        f"search results {total_hits} corroborated · "
        f"skipped {skipped} · errors {errors}"
        + (" · stopped early (DDG blocked)" if stopped_early else "")
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
    parser.add_argument(
        "--web",
        choices=WEB_MODES,
        default="all",
        help="DuckDuckGo mode: all wallets, only ENS-named pivots, or none",
    )
    args = parser.parse_args(argv)
    return run(limit=args.limit, web=args.web)


if __name__ == "__main__":
    raise SystemExit(main())
