"""Batch-scan token holder lists and aggregate unique target wallets.

For each ``SYMBOL:ADDRESS:CHAIN`` spec:

1. paginate Blockscout's keyless holders API into ``out/batch/SYMBOL_holders.csv``
2. run ``scan_target.bat`` with ``--quiet``
3. snapshot the rows that run appended to ``out/report_matches.csv`` into
   ``out/batch/SYMBOL_matches.csv``

Finally merge the per-token snapshots into ``out/master_targets.csv``, one row
per unique wallet, keeping its highest Total USD and the union of the tokens and
networks that surfaced it. Token contracts are never targets: an address that is
one of the scanned contracts (plus any ``--token`` address) is dropped from the
master, so a contract's own holdings cannot rank as a wallet.
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wallet_analyzer.tokens import DEFAULT_BEP20_TOKENS  # noqa: E402

FETCH_SCRIPT = Path(__file__).resolve().parent / "fetch_blockscout_holders.py"
SCAN_WRAPPER = ROOT / "scan_target.bat"
OUT_DIR = ROOT / "out"
BATCH_DIR = OUT_DIR / "batch"
REPORT_MATCHES = OUT_DIR / "report_matches.csv"
MASTER = OUT_DIR / "master_targets.csv"
EXCHANGE_REGISTRY = ROOT / "data" / "exchange_wallets.csv"

REPORT_COLUMNS = ("Wallet", "Network", "Native USD", "Token USD", "Total USD", "Status")
MASTER_COLUMNS = ("Wallet", "Tokens", "Networks", "Best Total USD", "Status")
MIN_USD = Decimal("50000")
MAX_USD = Decimal("900000")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_CHAINS = {"eth": "eth", "ethereum": "eth", "bsc": "bsc", "bnb": "bsc"}
#: Contracts this project screens, keyed by lowercase address. They hold their
#: own supply, so a holder list inevitably contains them; they are excluded from
#: the master. Two sources, because a contract shows up on every leg it is read
#: on: the Ethereum stablecoins ``run_pipeline`` scans, and the whole BEP-20
#: allowlist the analyzer prices for BSC wallets. ``--aggregate-only`` runs
#: without any ``--token`` specs, so this set has to stand alone (the tests keep
#: it a superset of both lists).
SCANNED_TOKENS = {
    **{token.key: token.symbol for token in DEFAULT_BEP20_TOKENS},
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
}


@dataclass(frozen=True)
class TokenSpec:
    symbol: str
    address: str
    chain: str


def parse_spec(value: str) -> TokenSpec:
    parts = value.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            f"expected SYMBOL:ADDRESS:CHAIN, got {value!r}"
        )
    symbol, address, chain = (part.strip() for part in parts)
    if not symbol:
        raise argparse.ArgumentTypeError("token symbol must not be empty")
    if not _ADDRESS.fullmatch(address):
        raise argparse.ArgumentTypeError(f"invalid token address: {address!r}")
    normalized_chain = _CHAINS.get(chain.lower())
    if normalized_chain is None:
        raise argparse.ArgumentTypeError(
            f"unsupported chain {chain!r}; expected ethereum/eth or bsc/bnb"
        )
    return TokenSpec(symbol.upper(), address, normalized_chain)


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def fetch_holders(spec: TokenSpec, limit: int, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            sys.executable,
            str(FETCH_SCRIPT),
            "--token",
            spec.address,
            "--limit",
            str(limit),
            "--out",
            str(out_path),
        ],
        cwd=ROOT,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"holder fetch failed for {spec.symbol} (exit {completed.returncode})"
        )


def run_scan(spec: TokenSpec, limit: int, holders_csv: Path, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            [
                "cmd",
                "/c",
                "call",
                str(SCAN_WRAPPER),
                spec.address,
                spec.chain,
                str(limit),
                str(holders_csv),
                "--quiet",
            ],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"scan failed for {spec.symbol} (exit {completed.returncode}); see {log_path}"
        )


def snapshot_matches(symbol: str, before: int) -> tuple[int, Path]:
    rows = read_rows(REPORT_MATCHES)[before:]
    path = BATCH_DIR / f"{symbol}_matches.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(REPORT_COLUMNS)
        for row in rows:
            writer.writerow([row.get(column, "") for column in REPORT_COLUMNS])
    return len(rows), path


def aggregate(
    paths: list[tuple[str, Path]],
    minimum: Decimal = MIN_USD,
    maximum: Decimal = MAX_USD,
    excluded: set[str] | None = None,
    skipped: set[str] | None = None,
) -> list[dict[str, object]]:
    """Merge snapshots into master rows, dropping scanned token contracts.

    ``excluded`` holds lowercase addresses to ignore (defaults to
    ``SCANNED_TOKENS``); each address actually dropped is added to ``skipped``.
    """

    contracts = set(SCANNED_TOKENS) if excluded is None else excluded
    merged: dict[str, dict[str, object]] = {}

    for symbol, path in paths:
        for row in read_rows(path):
            try:
                total = Decimal(row["Total USD"])
            except (KeyError, InvalidOperation):
                continue
            if not minimum <= total <= maximum:
                continue

            key = row["Wallet"].lower()
            if key in contracts:
                if skipped is not None:
                    skipped.add(key)
                continue

            entry = merged.get(key)
            if entry is None:
                entry = {
                    "wallet": row["Wallet"],
                    "tokens": set(),
                    "networks": set(),
                    "total": total,
                    "status": row["Status"],
                }
                merged[key] = entry

            entry["tokens"].add(symbol)  # type: ignore[union-attr]
            entry["networks"].add(row["Network"])  # type: ignore[union-attr]
            if total > entry["total"]:  # type: ignore[operator]
                entry["total"] = total
                entry["status"] = row["Status"]
                entry["wallet"] = row["Wallet"]

    return sorted(merged.values(), key=lambda entry: entry["total"], reverse=True)


def load_exchange_labels(path: Path = EXCHANGE_REGISTRY) -> dict[str, str]:
    """Curated exchange labels keyed by lowercase address.

    No keyless label API exists (Blockscout returns no name, Etherscan exposes no
    labels endpoint), so this registry is the label source: extend the CSV rather
    than guessing.
    """
    if not path.exists():
        return {}

    labels: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            address = (row.get("address") or "").strip().lower()
            if not _ADDRESS.fullmatch(address):
                continue
            label = (row.get("label") or "").strip()
            labels[address] = label or "exchange"
    return labels


def drop_exchange_wallets(
    entries: list[dict[str, object]], labels: dict[str, str]
) -> tuple[list[dict[str, object]], list[tuple[str, str]]]:
    """Split entries into (kept, dropped) using the exchange registry."""
    kept: list[dict[str, object]] = []
    dropped: list[tuple[str, str]] = []
    for entry in entries:
        wallet = str(entry["wallet"])
        label = labels.get(wallet.lower())
        if label is None:
            kept.append(entry)
        else:
            dropped.append((wallet, label))
    return kept, dropped


def write_master(entries: list[dict[str, object]]) -> None:
    MASTER.parent.mkdir(parents=True, exist_ok=True)
    with MASTER.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(MASTER_COLUMNS)
        for entry in entries:
            writer.writerow(
                [
                    entry["wallet"],
                    ",".join(sorted(entry["tokens"])),  # type: ignore[arg-type]
                    ",".join(sorted(entry["networks"])),  # type: ignore[arg-type]
                    f"{entry['total']:.2f}",  # type: ignore[arg-type]
                    entry["status"],
                ]
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--token",
        action="append",
        type=parse_spec,
        default=[],
        metavar="SYMBOL:ADDRESS:CHAIN",
        help="token to scan; repeatable, e.g. USDT:0x...:ethereum",
    )
    parser.add_argument(
        "--limit", type=int, default=1000, help="holders per token (default 1000)"
    )
    parser.add_argument(
        "--min-usd", type=Decimal, default=MIN_USD, help="lower bound (default 50000)"
    )
    parser.add_argument(
        "--max-usd", type=Decimal, default=MAX_USD, help="upper bound (default 900000)"
    )
    parser.add_argument(
        "--aggregate-only",
        action="store_true",
        help="skip fetching/scanning and rebuild master_targets.csv from out/batch",
    )
    parser.add_argument(
        "--reuse-holders",
        action="store_true",
        help=(
            "screen out/batch/<SYMBOL>_holders.csv without re-fetching, for when "
            "the holder API is unavailable"
        ),
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.min_usd >= args.max_usd:
        parser.error("--min-usd must be lower than --max-usd")

    paths: list[tuple[str, Path]] = []
    failures: list[str] = []

    if args.aggregate_only:
        paths = [
            (path.name[: -len("_matches.csv")], path)
            for path in sorted(BATCH_DIR.glob("*_matches.csv"))
        ]
        if not paths:
            print(f"error: no per-token snapshots under {BATCH_DIR}", file=sys.stderr)
            return 1
    else:
        specs: list[TokenSpec] = args.token
        if not specs:
            parser.error("--token is required unless --aggregate-only is set")
        symbols = [spec.symbol for spec in specs]
        if len(set(symbols)) != len(symbols):
            parser.error("token symbols must be unique")

        for index, spec in enumerate(specs, start=1):
            print(f"[{index}/{len(specs)}] {spec.symbol} {spec.address} on {spec.chain}")
            holders_csv = BATCH_DIR / f"{spec.symbol}_holders.csv"
            log_path = BATCH_DIR / f"{spec.symbol}.log"
            try:
                if args.reuse_holders:
                    if not holders_csv.exists():
                        raise RuntimeError(
                            f"--reuse-holders: missing holder file {holders_csv}"
                        )
                    print(f"  reusing holders: {holders_csv}")
                else:
                    fetch_holders(spec, args.limit, holders_csv)
                before = len(read_rows(REPORT_MATCHES))
                run_scan(spec, args.limit, holders_csv, log_path)
                count, match_path = snapshot_matches(spec.symbol, before)
            except (RuntimeError, OSError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                failures.append(spec.symbol)
                continue

            paths.append((spec.symbol, match_path))
            print(f"  matches: {count} -> {match_path}")

    if not args.aggregate_only and not paths:
        # A provider outage must never replace a good master with an empty one.
        print(
            "error: every token failed; keeping the existing master_targets.csv",
            file=sys.stderr,
        )
        return 1

    excluded = set(SCANNED_TOKENS) | {spec.address.lower() for spec in args.token}
    skipped: set[str] = set()
    entries = aggregate(
        paths, args.min_usd, args.max_usd, excluded=excluded, skipped=skipped
    )
    if skipped:
        print(f"token contracts excluded: {', '.join(sorted(skipped))}")

    entries, dropped = drop_exchange_wallets(entries, load_exchange_labels())
    if dropped:
        detail = ", ".join(f"{address} ({label})" for address, label in dropped)
        print(f"exchange wallets excluded: {detail}")

    write_master(entries)
    print(f"master: {MASTER} ({len(entries)} unique wallet(s))")

    if failures:
        print(f"failed tokens: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
