"""Fetch a token's top holders from Ethplorer's free API into a holder CSV.

Ethplorer replaces Dune/BscScan as the free holder source. Only the ``address``
field is kept; balances are left empty so the output matches the
``"HolderAddress","Balance","PendingBalanceUpdate"`` shape the discovery CSV
reader already understands.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import requests

API = "https://api.ethplorer.io/getTopTokenHolders/{token}"
DEFAULT_TOKEN = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
COLUMNS = ("HolderAddress", "Balance", "PendingBalanceUpdate")
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "real_eth_usdt_holders.csv"


def fetch_holders(token: str, limit: int, api_key: str) -> list[str]:
    response = requests.get(
        API.format(token=token),
        params={"apiKey": api_key, "limit": limit},
        timeout=30,
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message") or response.text[:200]
        except ValueError:
            detail = response.text[:200]
        raise SystemExit(
            f"Ethplorer rejected the request (HTTP {response.status_code}): {detail}"
        )
    payload = response.json()

    if "holders" not in payload:
        raise SystemExit(f"Ethplorer returned no holder list: {payload}")

    addresses: list[str] = []
    seen: set[str] = set()
    for holder in payload["holders"]:
        address = holder.get("address")
        if not isinstance(address, str) or not address:
            continue
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        addresses.append(address)
    return addresses


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="ERC-20 contract address")
    parser.add_argument("--limit", type=int, default=1000, help="holders to request")
    parser.add_argument("--api-key", default="freekey", help="Ethplorer API key")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="output CSV path")
    args = parser.parse_args()

    addresses = fetch_holders(args.token, args.limit, args.api_key)
    if not addresses:
        print("error: Ethplorer returned no usable addresses", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for address in addresses:
            writer.writerow([address, "", ""])

    print(f"holders: {len(addresses)}")
    print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
