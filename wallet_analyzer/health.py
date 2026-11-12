"""Connection tests for every external service the analyzer depends on.

Each check performs one real, read-only request against a well-known public
address (or the price feed) and reports what happened. No credential is ever
placed in a status line: endpoints are shown by host only, and provider
messages pass through :func:`wallet_analyzer.net.scrub`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from .config import Settings
from .models import Network
from .net import HttpClient, ProviderError, scrub
from .prices import PriceError, PriceService
from .providers import build_providers, describe_modes
from .providers.base import Provider
from .report import format_amount

OK = "OK"
NEEDS_CONFIG = "NEEDS CONFIG"
FAILED = "FAILED"

KNOWN_ADDRESSES: dict[Network, str] = {
    Network.BITCOIN: "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
    Network.ETHEREUM: "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    Network.BSC: "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    Network.SOLANA: "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
}


@dataclass(frozen=True)
class ServiceCheck:
    service: str
    status: str
    detail: str
    action: str = ""
    required: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def run_checks(
    settings: Settings,
    client: HttpClient | None = None,
    *,
    providers: dict[Network, Provider] | None = None,
    price_service: PriceService | None = None,
) -> list[ServiceCheck]:
    resolved = providers or build_providers(settings, client)
    checks = [
        _check_bitcoin(resolved, settings),
        _check_ethereum(resolved, settings),
    ]
    checks.extend(_check_bsc(resolved, settings))
    checks.append(_check_solana(resolved))
    checks.append(_check_prices(settings, client, price_service))
    return checks


def exit_code(checks: Iterable[ServiceCheck]) -> int:
    """Non-zero only when something required is actually broken."""

    return 1 if any(check.status == FAILED and check.required for check in checks) else 0


def count_by_status(checks: Iterable[ServiceCheck]) -> dict[str, int]:
    counts = {OK: 0, NEEDS_CONFIG: 0, FAILED: 0}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    return counts


def format_table(checks: list[ServiceCheck]) -> str:
    width = max(len(check.service) for check in checks) + 2
    lines = [f"{'SERVICE'.ljust(width)}{'STATUS'.ljust(13)}DETAIL"]
    lines.append(f"{'-' * width}{'-' * 12}  {'-' * 46}")
    for check in checks:
        lines.append(f"{check.service.ljust(width)}{check.status.ljust(13)}{check.detail}")
        if check.action and check.status != OK:
            lines.append(f"{' ' * width}{' ' * 13}  -> {check.action}")
    return "\n".join(lines)


def format_summary(checks: list[ServiceCheck]) -> str:
    counts = count_by_status(checks)
    parts = [f"{counts[OK]} OK"]
    if counts[NEEDS_CONFIG]:
        parts.append(f"{counts[NEEDS_CONFIG]} need configuration")
    if counts[FAILED]:
        parts.append(f"{counts[FAILED]} failed")
    return f"{len(checks)} services checked: " + ", ".join(parts)


# -- individual checks ---------------------------------------------------


def _check_bitcoin(providers: dict[Network, Provider], settings: Settings) -> ServiceCheck:
    label = "Bitcoin (Blockchain.com Explorer)"
    mode = "keyed" if settings.blockchain_api_key else "keyless"
    try:
        result = providers[Network.BITCOIN].fetch(KNOWN_ADDRESSES[Network.BITCOIN])
    except ProviderError as exc:
        return ServiceCheck(
            label,
            FAILED,
            str(exc),
            "check connectivity to blockchain.info; the API works without a key",
        )
    return ServiceCheck(
        label, OK, f"{mode}; balance {format_amount(result.balance)} BTC"
    )


def _check_ethereum(providers: dict[Network, Provider], settings: Settings) -> ServiceCheck:
    label = "Ethereum (Etherscan V2 / Blockscout)"
    provider = providers[Network.ETHEREUM]
    mode = describe_modes({Network.ETHEREUM: provider})[Network.ETHEREUM]
    try:
        result = provider.fetch(KNOWN_ADDRESSES[Network.ETHEREUM])
    except ProviderError as exc:
        action = (
            "ETHERSCAN_API_KEY was rejected: remove it to fall back to keyless Blockscout"
            if settings.has_etherscan_key
            else "check connectivity to the Ethereum data source"
        )
        return ServiceCheck(label, FAILED, str(exc), action)
    return ServiceCheck(
        label, OK, f"{mode}; balance {format_amount(result.balance)} ETH"
    )


def _check_bsc(
    providers: dict[Network, Provider], settings: Settings
) -> tuple[ServiceCheck, ServiceCheck]:
    balance_label = "BNB Smart Chain balance (JSON-RPC)"
    activity_label = "BNB Smart Chain activity (Etherscan V2)"

    try:
        result = providers[Network.BSC].fetch(KNOWN_ADDRESSES[Network.BSC])
    except ProviderError as exc:
        return (
            ServiceCheck(balance_label, FAILED, str(exc), "check BSC_RPC_URL"),
            ServiceCheck(
                activity_label,
                FAILED,
                "not checked because the balance call failed",
                "fix BSC_RPC_URL first",
                required=False,
            ),
        )

    balance_check = ServiceCheck(
        balance_label,
        OK,
        f"{result.balance_source}; balance {format_amount(result.balance)} BNB",
    )

    if result.activity_verified:
        return balance_check, ServiceCheck(
            activity_label, OK, f"{result.activity_source}; history readable"
        )

    reason = next(
        (note for note in result.notes if note.startswith("activity lookup failed")), ""
    )
    if not settings.has_bsc_activity_key:
        return balance_check, ServiceCheck(
            activity_label,
            NEEDS_CONFIG,
            "no key covers chainid=56; BNB wallets stay PARTIAL",
            "add BSCSCAN_API_KEY (paid Etherscan plan) to enable BNB Chain activity",
            required=False,
        )

    return balance_check, ServiceCheck(
        activity_label,
        FAILED,
        scrub(reason) or "the configured key could not read chainid=56",
        "confirm the key's plan covers BNB Smart Chain, or leave BSCSCAN_API_KEY empty",
        required=False,
    )


def _check_solana(providers: dict[Network, Provider]) -> ServiceCheck:
    label = "Solana (JSON-RPC)"
    provider = providers[Network.SOLANA]
    mode = describe_modes({Network.SOLANA: provider})[Network.SOLANA]
    try:
        result = provider.fetch(KNOWN_ADDRESSES[Network.SOLANA])
    except ProviderError as exc:
        return ServiceCheck(
            label, FAILED, str(exc), "check SOLANA_RPC_URL; the public endpoint throttles"
        )
    return ServiceCheck(label, OK, f"{mode}; balance {format_amount(result.balance)} SOL")


def _check_prices(
    settings: Settings, client: HttpClient, price_service: PriceService | None
) -> ServiceCheck:
    label = "Prices (CoinGecko, Coinbase fallback)"
    service = price_service or PriceService(client, ttl_seconds=0, cache_path=None)
    try:
        snapshot = service.get(force_refresh=True)
    except PriceError as exc:
        return ServiceCheck(
            label,
            FAILED,
            str(exc),
            "retry later; the analyzer refuses to value wallets without a price feed",
        )
    return ServiceCheck(
        label,
        OK,
        f"{snapshot.source}; BTC ${snapshot.prices['BTC']:,.2f}",
    )
