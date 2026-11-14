"""Run the whole multi-chain pipeline with one command.

    python scripts\\run_pipeline.py

Steps:

1. fetch the top holders of USDT/USDC/DAI and screen them (skipped with
   ``--reuse-evm``, which reuses the snapshots already under ``out/batch``)
2. build ``data/btc_sol_holders.csv`` from the curated BTC/SOL seed and screen it
3. aggregate every per-population snapshot into ``out/master_targets.csv``
4. render ``out/targets_report.html``

Each stage logs to ``out/batch/pipeline_*.log`` and the script stops on the first
failure with the failing stage named.
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
OUT = ROOT / "out"
BATCH = OUT / "batch"
REPORT_MATCHES = OUT / "report_matches.csv"
MASTER = OUT / "master_targets.csv"
HTML = OUT / "targets_report.html"
RUN_CMD = ROOT / "run.cmd"
BTC_SOL_CSV = ROOT / "data" / "btc_sol_holders.csv"

BATCH_SCANNER = SCRIPTS / "batch_scan_tokens.py"
BTC_SOL_FETCHER = SCRIPTS / "fetch_btc_sol_holders.py"
REPORT_GENERATOR = SCRIPTS / "generate_report.py"

TOKENS = (
    ("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", "ethereum"),
    ("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "ethereum"),
    ("DAI", "0x6B175474E89094C44Da98b954EedeAC495271d0F", "ethereum"),
)
REPORT_COLUMNS = ("Wallet", "Network", "Native USD", "Token USD", "Total USD", "Status")
#: Snapshot name per chain label, so the aggregator picks these up as populations.
CHAIN_SNAPSHOTS = {"Bitcoin": "BTC", "Solana": "SOL"}

VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PYTHON = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def run_step(label: str, command: list[str], log_path: Path) -> None:
    print(f"  -> {label}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT
        )
    elapsed = time.monotonic() - started
    if completed.returncode != 0:
        raise SystemExit(
            f"error: {label} failed (exit {completed.returncode}); see {log_path}"
        )
    print(f"     done in {elapsed:.0f}s ({log_path.name})")


def snapshot_chain_rows(rows: list[dict[str, str]]) -> int:
    """Split the freshly appended match rows into one snapshot per chain."""

    written = 0
    for network, symbol in CHAIN_SNAPSHOTS.items():
        selected = [row for row in rows if row.get("Network") == network]
        path = BATCH / f"{symbol}_matches.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(REPORT_COLUMNS)
            for row in selected:
                writer.writerow([row.get(column, "") for column in REPORT_COLUMNS])
        written += len(selected)
        print(f"     {network:<8} {len(selected)} match row(s) -> {path.name}")
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=1000, help="EVM holders per token (default 1000)"
    )
    parser.add_argument(
        "--min-usd", type=Decimal, default=Decimal("50000"), help="lower bound"
    )
    parser.add_argument(
        "--max-usd", type=Decimal, default=Decimal("900000"), help="upper bound"
    )
    parser.add_argument(
        "--reuse-evm",
        action="store_true",
        help="skip fetching/rescanning EVM tokens and reuse out/batch snapshots",
    )
    parser.add_argument(
        "--reuse-holders",
        action="store_true",
        help=(
            "screen the existing out/batch/<SYMBOL>_holders.csv instead of "
            "re-fetching, for when the holder API is down"
        ),
    )
    parser.add_argument(
        "--skip-btc-sol", action="store_true", help="leave Bitcoin/Solana untouched"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.min_usd >= args.max_usd:
        print("error: --min-usd must be lower than --max-usd", file=sys.stderr)
        return 2
    if args.limit < 1:
        print("error: --limit must be at least 1", file=sys.stderr)
        return 2

    started = time.monotonic()
    bounds = ["--min-usd", str(args.min_usd), "--max-usd", str(args.max_usd)]

    print("[1/4] EVM token holders")
    if args.reuse_evm:
        snapshots = sorted(BATCH.glob("*_matches.csv"))
        print(f"  reused {len(snapshots)} existing snapshot(s) under {BATCH}")
    else:
        command = [
            str(PYTHON),
            str(BATCH_SCANNER),
            "--limit",
            str(args.limit),
            *bounds,
        ]
        for symbol, address, chain in TOKENS:
            command += ["--token", f"{symbol}:{address}:{chain}"]
        if args.reuse_holders:
            command.append("--reuse-holders")
        run_step(
            "fetch + screen USDT/USDC/DAI",
            command,
            BATCH / "pipeline_evm.log",
        )

    print("[2/4] Bitcoin and Solana holders")
    if args.skip_btc_sol:
        print("  skipped (--skip-btc-sol)")
    else:
        run_step(
            "build the curated BTC/SOL list",
            [str(PYTHON), str(BTC_SOL_FETCHER), "--out", str(BTC_SOL_CSV)],
            BATCH / "pipeline_btc_sol_fetch.log",
        )
        holders = read_rows(BTC_SOL_CSV)
        if not holders:
            print("  warning: no BTC/SOL addresses were kept; nothing to screen")
        else:
            before = len(read_rows(REPORT_MATCHES))
            run_step(
                f"screen {len(holders)} BTC/SOL address(es)",
                [
                    "cmd",
                    "/c",
                    "call",
                    str(RUN_CMD),
                    "analyze",
                    "--file",
                    str(BTC_SOL_CSV),
                    "--filter",
                    "--quiet",
                    *bounds,
                ],
                BATCH / "pipeline_btc_sol_scan.log",
            )
            appended = read_rows(REPORT_MATCHES)[before:]
            snapshot_chain_rows(appended)

    print("[3/4] aggregate master_targets.csv")
    run_step(
        "merge every population snapshot",
        [str(PYTHON), str(BATCH_SCANNER), "--aggregate-only", *bounds],
        BATCH / "pipeline_aggregate.log",
    )

    print("[4/4] render the HTML report")
    run_step(
        "generate targets_report.html",
        [str(PYTHON), str(REPORT_GENERATOR), *bounds],
        BATCH / "pipeline_report.log",
    )

    entries = read_rows(MASTER)
    chains: dict[str, int] = {}
    for entry in entries:
        for network in (entry.get("Networks") or "").split(","):
            if network:
                chains[network] = chains.get(network, 0) + 1

    print()
    print(f"pipeline complete in {time.monotonic() - started:.0f}s")
    print(f"  master : {MASTER} ({len(entries)} wallet(s))")
    for network, count in sorted(chains.items()):
        print(f"    {network:<16} {count}")
    print(f"  report : {HTML}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
