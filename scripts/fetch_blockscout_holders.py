"""Fetch a token's top holders from Blockscout's keyless API into a holder CSV.

Ethplorer's free key caps ``getTopTokenHolders`` at 100, so paginating Blockscout
is the free path to 1,000+ Ethereum holders. Only ``address.hash`` is kept;
balances are left empty so the output matches the discovery CSV reader.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import requests

API = "https://eth.blockscout.com/api/v2/tokens/{token}/holders"
DEFAULT_TOKEN = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
COLUMNS = ("HolderAddress", "Balance", "PendingBalanceUpdate")
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent / "data" / "real_eth_usdt_holders.csv"
)


def fetch_holders(token: str, limit: int, pause: float) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    params: dict[str, object] | None = None

    while len(addresses) < limit:
        response = requests.get(
            API.format(token=token), params=params, timeout=30
        )
        response.raise_for_status()
        payload = response.json()

        items = payload.get("items") or []
        if not items:
            break

        added = 0
        for item in items:
            address = (item.get("address") or {}).get("hash")
            if not isinstance(address, str) or not address:
                continue
            key = address.lower()
            if key in seen:
                continue
            seen.add(key)
            addresses.append(address)
            added += 1
            if len(addresses) >= limit:
                break

        next_params = payload.get("next_page_params")
        if added == 0 or not next_params:
            break
        params = next_params
        time.sleep(pause)

    return addresses


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="ERC-20 contract address")
    parser.add_argument("--limit", type=int, default=1000, help="holders to request")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="output CSV path")
    parser.add_argument(
        "--pause", type=float, default=0.3, help="seconds between holder pages"
    )
    args = parser.parse_args()

    addresses = fetch_holders(args.token, args.limit, args.pause)
    if not addresses:
        print("error: Blockscout returned no usable addresses", file=sys.stderr)
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
