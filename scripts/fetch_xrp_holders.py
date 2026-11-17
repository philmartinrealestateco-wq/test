"""Build ``out/xrp_holders.csv`` from XRPScan's keyless top-accounts list.

XRPScan's ``/api/v1/balances`` endpoint returns the top 10,000 active XRP
accounts in descending balance order, refreshed nightly. It is the free rich
list this project uses for XRP discovery.

Custodial and escrow accounts are filtered out by their published labels,
because an exchange hot wallet is not a useful OSINT target. Pass
``--include-labeled`` to keep them. An exported list can be screened with
``--input`` instead of the live endpoint; rows run through the same validation.
"""

from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wallet_analyzer.models import Network  # noqa: E402
from wallet_analyzer.validators import validate  # noqa: E402

BALANCES_URL = "https://api.xrpscan.com/api/v1/balances"
PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
DEFAULT_OUT = ROOT / "out" / "xrp_holders.csv"
COLUMNS = ("address", "network", "balance", "label")
DROPS_PER_XRP = Decimal(10) ** 6
MAX_ACCOUNTS = 10_000
DEFAULT_MIN_USD = Decimal("10000")
DEFAULT_MAX_USD = Decimal("990000")

CUSTODIAL_MARKERS = (
    "binance",
    "coinbase",
    "kraken",
    "bitstamp",
    "okx",
    "bybit",
    "huobi",
    "htx",
    "gate",
    "kucoin",
    "bitfinex",
    "gemini",
    "crypto.com",
    "upbit",
    "bittrex",
    "ripple",
    "escrow",
    "custody",
    "cold wallet",
    "hot wallet",
    "exchange",
    "robinhood",
    "bithumb",
    "bitbank",
    "coincheck",
    "etoro",
    "coinone",
    "bitgo",
    "anchorage",
    "fireblocks",
    "copper",
    "bitpanda",
    "bitvavo",
    "okcoin",
    "poloniex",
    "mexc",
    "bitget",
    "lbank",
    "probit",
    "ascendex",
    "bitrue",
    "coinex",
    "hitbtc",
    "deribit",
)


def _label(entry: dict) -> str:
    name = entry.get("name")
    if not isinstance(name, dict):
        return ""
    for key in ("name", "desc", "domain", "twitter"):
        value = name.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_custodial(entry: dict, label: str) -> bool:
    if entry.get("escrow"):
        return True
    text = label.lower()
    return any(marker in text for marker in CUSTODIAL_MARKERS)


def fetch_balances(
    limit: int,
    *,
    include_labeled: bool = False,
    minimum: Decimal = DEFAULT_MIN_USD,
    maximum: Decimal = DEFAULT_MAX_USD,
) -> tuple[list[tuple[str, str, str, str]], int, int, int]:
    response = requests.get(
        BALANCES_URL,
        timeout=60,
        headers={"User-Agent": "wallet-analyzer/1.0 (read-only public data)"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("XRPScan returned an unexpected balances payload")

    price = _xrp_usd()
    candidates: list[tuple[Decimal, str, str]] = []
    invalid = 0
    custodial = 0
    out_of_range = 0
    seen: set[str] = set()

    for entry in payload:
        if not isinstance(entry, dict):
            continue
        account = str(entry.get("account") or "").strip()
        if not account or account in seen:
            continue

        outcome = validate(Network.XRP, account)
        if not outcome.valid:
            invalid += 1
            continue

        try:
            drops = int(entry.get("balance") or 0)
        except (TypeError, ValueError):
            invalid += 1
            continue
        if drops <= 0:
            continue

        xrp = Decimal(drops) / DROPS_PER_XRP
        usd = xrp * price
        if not minimum <= usd <= maximum:
            out_of_range += 1
            continue

        label = _label(entry)
        if not include_labeled and _is_custodial(entry, label):
            custodial += 1
            continue

        seen.add(account)
        candidates.append((xrp, outcome.normalized, label))

    candidates.sort(key=lambda item: item[0], reverse=True)
    rows = [
        (account, Network.XRP.value, f"{xrp:.6f}", label)
        for xrp, account, label in candidates[:limit]
    ]
    return rows, invalid, custodial, out_of_range


def _xrp_usd() -> Decimal:
    response = requests.get(
        PRICE_URL,
        params={"ids": "ripple", "vs_currencies": "usd"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    try:
        price = Decimal(str(payload["ripple"]["usd"]))
    except (KeyError, TypeError, InvalidOperation) as error:
        raise ValueError("CoinGecko returned no usable XRP price") from error
    if price <= 0:
        raise ValueError("CoinGecko returned a non-positive XRP price")
    return price


def load_input(path: Path, limit: int) -> tuple[list[tuple[str, str, str, str]], int]:
    rows: list[tuple[str, str, str, str]] = []
    invalid = 0

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        if path.suffix.lower() in {".csv", ".tsv"}:
            reader = csv.DictReader(
                handle, delimiter="\t" if path.suffix.lower() == ".tsv" else ","
            )
            for row in reader:
                lookup = {name.strip().lower(): value for name, value in row.items() if name}
                address = (lookup.get("address") or lookup.get("account") or "").strip()
                network = (lookup.get("network") or lookup.get("chain") or "").strip()
                label = (lookup.get("label") or lookup.get("name") or "").strip()
                balance = (lookup.get("balance") or "").strip()
                if not address:
                    continue
                if network and network.lower() not in {"xrp", "ripple", "xrpl"}:
                    invalid += 1
                    continue
                outcome = validate(Network.XRP, address)
                if not outcome.valid:
                    invalid += 1
                    continue
                rows.append((outcome.normalized, Network.XRP.value, balance, label))
                if len(rows) >= limit:
                    break
        else:
            for line in handle:
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                parts = [part.strip() for part in text.split(",")]
                address = parts[0]
                outcome = validate(Network.XRP, address)
                if not outcome.valid:
                    invalid += 1
                    continue
                rows.append(
                    (
                        outcome.normalized,
                        Network.XRP.value,
                        parts[1] if len(parts) > 1 else "",
                        parts[2] if len(parts) > 2 else "",
                    )
                )
                if len(rows) >= limit:
                    break

    return rows, invalid


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=2000,
        help=f"top XRP accounts to keep (default 2000, max {MAX_ACCOUNTS})",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output CSV")
    parser.add_argument(
        "--input",
        type=Path,
        help="exported address list instead of the live XRPScan rich list",
    )
    parser.add_argument(
        "--include-labeled",
        action="store_true",
        help="keep labelled custodial/escrow accounts instead of filtering them",
    )
    parser.add_argument(
        "--min-usd",
        type=Decimal,
        default=DEFAULT_MIN_USD,
        help="lower USD bound for the rich-list pre-filter (default 10000)",
    )
    parser.add_argument(
        "--max-usd",
        type=Decimal,
        default=DEFAULT_MAX_USD,
        help="upper USD bound for the rich-list pre-filter (default 990000)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.limit <= MAX_ACCOUNTS:
        print(f"error: --limit must be between 1 and {MAX_ACCOUNTS}", file=sys.stderr)
        return 2
    if args.min_usd >= args.max_usd:
        print("error: --min-usd must be lower than --max-usd", file=sys.stderr)
        return 2

    try:
        if args.input:
            rows, invalid = load_input(args.input, args.limit)
            source = f"exported list {args.input}"
            custodial = 0
            out_of_range = 0
        else:
            rows, invalid, custodial, out_of_range = fetch_balances(
                args.limit,
                include_labeled=args.include_labeled,
                minimum=args.min_usd,
                maximum=args.max_usd,
            )
            source = BALANCES_URL
    except (OSError, ValueError, requests.RequestException) as error:
        print(f"error: could not build the XRP holder list: {error}", file=sys.stderr)
        return 1

    if not rows:
        print("error: no valid XRP account was kept", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(rows)

    print(f"source  : {source}")
    print(f"window  : ${args.min_usd:,.0f} - ${args.max_usd:,.0f}")
    print(f"kept    : {len(rows)}")
    print(f"skipped : {invalid} invalid")
    if out_of_range:
        print(f"          {out_of_range} outside the USD window")
    if custodial:
        print(f"          {custodial} labelled custodial/escrow")
    print(f"written : {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
