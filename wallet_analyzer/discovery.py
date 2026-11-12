"""Token-holder discovery for populating ``--file`` input lists.

Primary source: Etherscan V2 ``tokenholderlist``, which is a Pro endpoint. On the
free plan it answers with an error, so a local CSV export (Dune and similar)
is a first-class fallback rather than an afterthought: the first column is read
and anything that is not a 20-byte hex address is skipped, which drops headers
and notes automatically.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import ConfigError
from .crypto import to_checksum_address
from .net import HttpClient, ProviderError, scrub

LOG = logging.getLogger(__name__)

ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"

CHAIN_IDS = {
    "bsc": 56,
    "bnb": 56,
    "binance": 56,
    "ethereum": 1,
    "eth": 1,
}

#: Documented key first; the alternates tolerate Etherscan-compatible providers.
_ADDRESS_KEYS = ("TokenHolderAddress", "tokenHolderAddress", "address", "holder", "wallet")


class DiscoveryError(RuntimeError):
    """No holder list could be produced from either source."""


def normalize_address(value: str) -> str | None:
    """Return the EIP-55 form of a 20-byte hex address, or ``None``.

    Case is not enforced: exports routinely contain lowercase addresses and the
    checksum is optional on-chain, so format validity (``0x`` + 40 hex) is the
    test here. The normalized output makes the file safe for ``analyze``.
    """

    text = value.strip().strip('"').strip("'")
    if not text.lower().startswith("0x") or len(text) != 42:
        return None
    try:
        int(text[2:], 16)
    except ValueError:
        return None
    return to_checksum_address(text)


def dedupe(addresses: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for address in addresses:
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(address)
    return unique


def extract_addresses(values: Iterable[str]) -> list[str]:
    normalized = [normalize_address(value) for value in values]
    return dedupe([address for address in normalized if address is not None])


def fetch_token_holders(
    client: HttpClient,
    api_key: str,
    contract: str,
    *,
    chain_id: int,
    offset: int = 100,
    page: int = 1,
) -> list[str]:
    """Call Etherscan V2 ``tokenholderlist`` and return holder addresses."""

    payload = client.get_json(
        ETHERSCAN_V2_URL,
        {
            "chainid": chain_id,
            "module": "token",
            "action": "tokenholderlist",
            "contractaddress": contract,
            "page": page,
            "offset": offset,
            "apikey": api_key,
        },
        host="api.etherscan.io",
    )

    if not isinstance(payload, dict):
        raise ProviderError(
            "Etherscan tokenholderlist returned an unexpected payload",
            host="api.etherscan.io",
        )

    if str(payload.get("status")) != "1":
        detail = payload.get("result") or payload.get("message") or "unknown error"
        raise ProviderError(f"Etherscan tokenholderlist: {detail}", host="api.etherscan.io")

    rows = payload.get("result")
    if not isinstance(rows, list):
        raise ProviderError(
            "Etherscan tokenholderlist returned no holder array", host="api.etherscan.io"
        )

    values: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in _ADDRESS_KEYS:
            if key in row:
                values.append(str(row[key]))
                break

    addresses = extract_addresses(values)
    if not addresses:
        raise ProviderError(
            "Etherscan tokenholderlist returned no usable holder addresses",
            host="api.etherscan.io",
        )
    return addresses


def read_holders_csv(path: Path, *, limit: int | None = None) -> list[str]:
    """Read wallet addresses from the first column of a CSV export.

    ``limit`` caps the number of unique addresses returned, so ``--limit`` means
    the same thing for an export as it does for the Etherscan request. Rows are
    streamed, so a large export is not parsed past the cap.
    """

    if limit is not None and limit < 1:
        raise DiscoveryError("limit must be at least 1")

    if not path.exists():
        raise DiscoveryError(f"CSV file not found: {path}")
    if path.is_dir():
        raise DiscoveryError(f"CSV path is a directory: {path}")

    addresses: list[str] = []
    seen: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                if limit is not None and len(addresses) >= limit:
                    break
                if not row:
                    continue
                normalized = normalize_address(row[0])
                if normalized is None or normalized.lower() in seen:
                    continue
                seen.add(normalized.lower())
                addresses.append(normalized)
    except OSError as exc:
        raise DiscoveryError(f"could not read {path.name}: {exc}") from exc

    if not addresses:
        raise DiscoveryError(
            f"{path.name} has no wallet addresses in its first column"
        )
    return addresses


def discover_holders(
    api_fetch: Callable[[], list[str]],
    *,
    csv_path: Path | None = None,
    limit: int | None = None,
) -> tuple[list[str], str]:
    """Try the API, then the CSV. Returns ``(addresses, source_label)``."""

    try:
        addresses = api_fetch()
    except (ProviderError, ConfigError) as exc:
        reason = scrub(str(exc))
        LOG.warning("token holder API unavailable: %s", reason)
        if csv_path is None:
            raise DiscoveryError(
                f"token holder API unavailable ({reason}); pass --csv with a local export"
            ) from exc
        return read_holders_csv(csv_path, limit=limit), f"CSV fallback ({csv_path.name})"

    return addresses, "Etherscan V2 tokenholderlist"


def write_holders(path: Path, addresses: Sequence[str]) -> int:
    """Write one address per line, ready for ``analyze --file``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(addresses) + "\n", encoding="utf-8")
    return len(addresses)
