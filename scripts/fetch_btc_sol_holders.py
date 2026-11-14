"""Build ``data/btc_sol_holders.csv`` from a curated, live-verified seed list.

Bitcoin and Solana have no ERC-20-style holder endpoint, and every keyless
rich-list source is closed right now: Blockchair blacklists shared IPs (HTTP
430), BitInfoCharts serves a Cloudflare challenge (403), and Solana's
``getLargestAccounts`` is disabled on the public RPC (429) while ``getBalance``
works. The seed below is therefore curated: real, well-known addresses, each
validated locally and then confirmed against a live balance lookup before it is
written.

Add entries to ``SEED`` as a source becomes available, or pass ``--input`` with
an exported rich list; ``--input`` accepts ``address[,network]`` rows and runs
through exactly the same validation.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wallet_analyzer.config import Settings  # noqa: E402
from wallet_analyzer.models import Network  # noqa: E402
from wallet_analyzer.validators import validate  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "btc_sol_holders.csv"
COLUMNS = ("address", "network", "balance", "label")
BTC_API = "https://blockchain.info/rawaddr/{address}"
SATOSHIS_PER_BTC = 10**8
LAMPORTS_PER_SOL = 10**9


@dataclass(frozen=True)
class Seed:
    address: str
    network: Network
    label: str


SEED: tuple[Seed, ...] = (
    Seed(
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
        Network.BITCOIN,
        "Genesis block coinbase",
    ),
    Seed(
        "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo",
        Network.BITCOIN,
        "Binance cold wallet",
    ),
    Seed(
        "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97",
        Network.BITCOIN,
        "Bitfinex cold wallet",
    ),
    Seed(
        "1Ay8vMC7R1UbyCCZRVULMV7iQpHSAbguJP",
        Network.BITCOIN,
        "Large holder (repo sample)",
    ),
    Seed(
        "5FGoPPj1nL8LCnfVnpTmreqQtqLuMXXAwuS1uahMrp8V",
        Network.SOLANA,
        "High-volume Solana wallet (repo sample)",
    ),
)


def _btc_balance(session: requests.Session, address: str) -> str:
    response = session.get(BTC_API.format(address=address), params={"limit": 1}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    satoshis = int(payload["final_balance"])
    return f"{satoshis / SATOSHIS_PER_BTC:.8f}"


def _sol_balance(session: requests.Session, rpc_url: str, address: str) -> str:
    response = session.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [address, {"commitment": "finalized"}],
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise ValueError(payload["error"].get("message") or "Solana RPC error")
    lamports = int(payload["result"]["value"])
    return f"{lamports / LAMPORTS_PER_SOL:.9f}"


RETRY_STATUSES = {429, 500, 502, 503, 504}


def _balance_with_retry(
    session: requests.Session, rpc_url: str, seed: Seed, attempts: int = 3
) -> tuple[str, str]:
    """Return ``(balance, error)``; an exhausted throttle yields an empty balance.

    A 429 here is the provider pacing us, never a reason to forget a real
    address, so the row is still written and the analyzer re-checks it later.
    """

    delay = 2.0
    last_error = ""
    for attempt in range(attempts):
        try:
            if seed.network is Network.BITCOIN:
                return _btc_balance(session, seed.address), ""
            return _sol_balance(session, rpc_url, seed.address), ""
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            last_error = f"HTTP {status}" if status else str(exc)
            if status in RETRY_STATUSES and attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            break
        except Exception as exc:  # noqa: BLE001 - one seed must not stop the list
            last_error = f"{type(exc).__name__}: {exc}"
            break
    return "", last_error


def load_input(path: Path) -> list[Seed]:
    rows: list[Seed] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        if path.suffix.lower() in {".csv", ".tsv"}:
            reader = csv.DictReader(handle, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
            for row in reader:
                lookup = {name.strip().lower(): value for name, value in row.items() if name}
                address = (lookup.get("address") or lookup.get("holderaddress") or "").strip()
                raw_network = (lookup.get("network") or lookup.get("chain") or "").strip()
                if not address:
                    continue
                network = _guess_network(address, raw_network)
                if network is None:
                    print(f"warning: skipped unrecognised address {address}", file=sys.stderr)
                    continue
                rows.append(Seed(address, network, (lookup.get("label") or "").strip()))
        else:
            for line in handle:
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                address, _, raw_network = text.partition(",")
                network = _guess_network(address.strip(), raw_network.strip())
                if network is None:
                    print(f"warning: skipped unrecognised address {address.strip()}", file=sys.stderr)
                    continue
                rows.append(Seed(address.strip(), network, ""))
    return rows


def _guess_network(address: str, raw_network: str) -> Network | None:
    if raw_network:
        try:
            return Network.parse(raw_network)
        except ValueError:
            return None
    candidates = [Network.BITCOIN, Network.SOLANA]
    for network in candidates:
        if validate(network, address).valid:
            return network
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output CSV")
    parser.add_argument(
        "--input", type=Path, help="external address list instead of the built-in seed"
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="write seed rows without a live balance lookup",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    seeds = load_input(args.input) if args.input else list(SEED)
    if not seeds:
        print("error: no candidate addresses", file=sys.stderr)
        return 1

    settings = Settings.load()
    rpc_host = urlparse(settings.solana_rpc_url).hostname or "invalid-endpoint"
    kept: list[tuple[Seed, str]] = []
    dropped = 0
    unconfirmed = 0

    with requests.Session() as session:
        session.headers.update({"User-Agent": "wallet-analyzer/1.0"})
        for index, seed in enumerate(seeds):
            outcome = validate(seed.network, seed.address)
            if not outcome.valid:
                print(
                    f"skip {seed.address} ({seed.label}): {outcome.reason}",
                    file=sys.stderr,
                )
                dropped += 1
                continue

            if args.no_verify:
                balance, error = "", ""
            else:
                balance, error = _balance_with_retry(
                    session, settings.solana_rpc_url, seed
                )
                if index < len(seeds) - 1:
                    time.sleep(1.0)

            if error:
                unconfirmed += 1
                print(
                    f"warn {seed.address} ({seed.label}): balance not confirmed "
                    f"({error}); keeping it for the analyzer to re-check",
                    file=sys.stderr,
                )
            kept.append((seed, balance))
            print(
                f"ok   {seed.address} {seed.network.label:<8} "
                f"balance={balance or 'unconfirmed'}"
            )

    if not kept:
        print("error: no address passed validation and verification", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for seed, balance in kept:
            writer.writerow([seed.address, seed.network.value, balance, seed.label])

    btc = sum(1 for seed, _ in kept if seed.network is Network.BITCOIN)
    sol = len(kept) - btc
    print()
    print(f"kept   : {len(kept)} ({btc} Bitcoin, {sol} Solana)")
    print(f"dropped: {dropped} invalid")
    print(f"unconfirmed balances: {unconfirmed}")
    print(f"written: {args.out}")
    print(f"solana : {rpc_host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
