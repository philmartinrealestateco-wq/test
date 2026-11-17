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

import requests
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
XRPSCAN_ACCOUNT_URL = "https://api.xrpscan.com/api/v1/account/{address}"
XRP_SEARCH_QUERIES = (
    "{address} xrpscan",
    "{address} bithomp",
    "{address} xrp wallet",
)

_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}$")
_XRP_ADDRESS = re.compile(r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$")


@dataclass(frozen=True)
class Target:
    address: str
    best_usd: str
    networks: str
    status: str
    kind: str = "evm"


@dataclass(frozen=True)
class EnsRecord:
    name: str
    twitter: str
    email: str
    url: str


@dataclass(frozen=True)
class XrpIdentity:
    address: str
    label: str = ""
    label_desc: str = ""
    label_domain: str = ""
    label_twitter: str = ""
    label_verified: bool = False
    parent_label: str = ""
    parent_desc: str = ""
    parent_domain: str = ""
    parent_account: str = ""
    parent_verified: bool = False
    domain_record: str = ""

    def finding_rows(self) -> list[tuple[str, str | None, str]]:
        """XRPScan rows as (finding, url, confidence)."""

        account_url = f"https://xrpscan.com/account/{self.address}"
        rows: list[tuple[str, str | None, str]] = []

        if self.label:
            label_url = (
                f"https://{self.label_domain}" if self.label_domain else account_url
            )
            suffix = f" ({self.label_desc})" if self.label_desc else ""
            rows.append(
                (
                    f"XRPScan label: {self.label}{suffix}",
                    label_url,
                    "VERIFIED" if self.label_verified else "UNVERIFIED",
                )
            )

        if self.label_twitter:
            rows.append(
                (
                    f"XRPScan twitter: @{self.label_twitter}",
                    f"https://twitter.com/{self.label_twitter}",
                    "VERIFIED" if self.label_verified else "UNVERIFIED",
                )
            )

        if self.domain_record:
            rows.append(
                (
                    f"XRPScan domain record: {self.domain_record}",
                    f"https://{self.domain_record}",
                    "VERIFIED",
                )
            )

        if self.parent_label:
            parent_url = (
                f"https://{self.parent_domain}"
                if self.parent_domain
                else (
                    f"https://xrpscan.com/account/{self.parent_account}"
                    if self.parent_account
                    else account_url
                )
            )
            suffix = f" ({self.parent_desc})" if self.parent_desc else ""
            rows.append(
                (
                    f"XRPScan parent label: {self.parent_label}{suffix}",
                    parent_url,
                    "VERIFIED" if self.parent_verified else "UNVERIFIED",
                )
            )

        return rows


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


def should_search_xrp(web: str, identity: XrpIdentity | None) -> bool:
    """XRP DuckDuckGo searches run only when there is an on-chain domain pivot."""

    if web == "all":
        return True
    if web == "pivots":
        return bool(identity and identity.domain_record)
    if web == "none":
        return False
    raise ValueError(f"unknown web mode {web!r}; expected one of {', '.join(WEB_MODES)}")


def read_targets(path: Path = MASTER_CSV, limit: int | None = DEFAULT_LIMIT) -> list[Target]:
    """Return EVM and classic XRP wallets from the master CSV."""
    if not path.exists():
        raise SystemExit(f"error: {path} not found; run scripts\\run_pipeline.py first")

    targets: list[Target] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            address = (row.get("Wallet") or "").strip()
            if _EVM_ADDRESS.match(address):
                kind = "evm"
                normalized = Web3.to_checksum_address(address)
            elif _XRP_ADDRESS.match(address):
                kind = "xrp"
                normalized = address
            else:
                continue

            targets.append(
                Target(
                    address=normalized,
                    best_usd=(row.get("Best Total USD") or "").strip(),
                    networks=(row.get("Networks") or "").strip(),
                    status=(row.get("Status") or "").strip(),
                    kind=kind,
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


def _name_value(value: object, key: str) -> str:
    if isinstance(value, dict):
        return str(value.get(key) or "").strip()
    if isinstance(value, str):
        return value.strip()
    return ""


def _name_verified(value: object) -> bool:
    return bool(value.get("verified")) if isinstance(value, dict) else False


def _decode_domain(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if "." in text and " " not in text:
        return text
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return ""
    decoded = raw.decode("utf-8", errors="replace").strip("\x00").strip()
    return decoded if "." in decoded and " " not in decoded else ""


def parse_xrp_identity(address: str, payload: dict) -> XrpIdentity:
    """Turn XRPScan's account labels and settings into identity findings."""

    account = payload.get("accountName") or {}
    parent = payload.get("parentName") or {}
    settings = payload.get("settings") or {}

    label = _name_value(account, "name") or _name_value(account, "username")
    parent_label = _name_value(parent, "name") or _name_value(parent, "username")

    return XrpIdentity(
        address=address,
        label=label,
        label_desc=_name_value(account, "desc"),
        label_domain=_name_value(account, "domain"),
        label_twitter=_name_value(account, "twitter").lstrip("@"),
        label_verified=_name_verified(account),
        parent_label=parent_label,
        parent_desc=_name_value(parent, "desc"),
        parent_domain=_name_value(parent, "domain"),
        parent_account=_name_value(parent, "account"),
        parent_verified=_name_verified(parent),
        domain_record=_decode_domain(
            settings.get("Domain") if isinstance(settings, dict) else ""
        ),
    )


def fetch_xrp_identity(address: str) -> XrpIdentity:
    """Read one XRP account's XRPScan label/domain with bounded retries."""

    last_error: object = None
    for attempt in range(3):
        try:
            response = requests.get(
                XRPSCAN_ACCOUNT_URL.format(address=address),
                timeout=20,
                headers={"User-Agent": "wallet-analyzer/1.0 (read-only public data)"},
            )
        except requests.RequestException as error:
            last_error = error
        else:
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = RuntimeError(f"HTTP {response.status_code}")
            else:
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("XRPScan returned an unexpected account payload")
                return parse_xrp_identity(address, payload)

        if attempt < 2:
            time.sleep(0.8 * (2**attempt))

    raise RuntimeError(f"XRPScan account lookup failed: {last_error}")


def search_address(address: str, results: int = SEARCH_RESULTS) -> list[SearchHit]:
    return _search_queries(address, SEARCH_QUERIES, results)


def search_xrp_address(address: str, results: int = SEARCH_RESULTS) -> list[SearchHit]:
    return _search_queries(address, XRP_SEARCH_QUERIES, results)


def _search_queries(
    address: str,
    templates: tuple[str, ...],
    results: int = SEARCH_RESULTS,
) -> list[SearchHit]:
    """Run a DuckDuckGo pivot, keeping only hits that name the address.

    A bare ``"<address> wallet"`` query returns unrelated filler when nothing
    matches, so each candidate is checked against the address and only
    explorer-scoped queries are added on top of the exact-phrase search.
    """
    hits: dict[str, SearchHit] = {}
    failures: list[str] = []

    for template in templates:
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

    if failures and len(failures) == len(templates):
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


def store_findings_typed(
    db,
    address: str,
    tool: str,
    rows: list[tuple[str, str | None, str]],
) -> None:
    """Replace rows for ``tool`` when each row carries its own confidence."""

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
        [
            (address, tool, finding, url, confidence, now)
            for finding, url, confidence in rows
        ],
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
    found_xrp = 0
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
            xrp_identity: XrpIdentity | None = None
            hits: list[SearchHit] = []
            search_enabled = False
            search_reason = "no ENS pivot"

            if target.kind == "xrp":
                print("      chain    : XRP")
                try:
                    xrp_identity = fetch_xrp_identity(target.address)
                except Exception as error:  # noqa: BLE001 - report, never guess
                    failures.append(
                        f"XRPScan {type(error).__name__}: {str(error)[:90]}"
                    )
                    print(
                        f"      XRPScan  : ERROR ({type(error).__name__}: {str(error)[:90]})"
                    )
                else:
                    rows = xrp_identity.finding_rows()
                    store_findings_typed(db, target.address, "XRPScan", rows)
                    if rows:
                        found_xrp += 1
                        for finding, url, _confidence in rows:
                            print(f"      XRPScan  : {finding}")
                            if url:
                                print(f"                 {url}")
                    else:
                        print("      XRPScan  : no label or domain record")
                search_enabled = should_search_xrp(web, xrp_identity)
                search_reason = "XRPScan identity only"
            else:
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
                search_enabled = should_search_address(web, record)

            if not search_enabled:
                print(f"      DDG      : skipped ({search_reason})")
            else:
                try:
                    hits = (
                        search_xrp_address(target.address)
                        if target.kind == "xrp"
                        else search_address(target.address)
                    )
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
        f"XRP identities {found_xrp} · search results {total_hits} corroborated · "
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
