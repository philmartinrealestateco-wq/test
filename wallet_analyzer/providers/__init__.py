"""Provider selection.

The choice is explicit and reported to the user: which source produced each
number matters more than picking the cleverest fallback chain.
"""

from __future__ import annotations

from ..config import ConfigError, Settings
from ..models import Network
from ..net import HttpClient
from .base import Provider
from .bitcoin import BitcoinProvider
from .evm import BlockscoutProvider, BscProvider, EtherscanV2Provider
from .solana import SolanaProvider

ETHERSCAN_CHAIN_IDS = {Network.ETHEREUM: 1, Network.BSC: 56}


def build_providers(settings: Settings, client: HttpClient) -> dict[Network, Provider]:
    providers: dict[Network, Provider] = {
        Network.BITCOIN: BitcoinProvider(client, settings.blockchain_api_key),
        Network.SOLANA: SolanaProvider(client, settings.solana_rpc_url),
    }

    if settings.has_etherscan_key:
        api_key = settings.etherscan_api_key or ""
        providers[Network.ETHEREUM] = EtherscanV2Provider(
            client,
            network=Network.ETHEREUM,
            chain_id=ETHERSCAN_CHAIN_IDS[Network.ETHEREUM],
            api_key=api_key,
        )
    elif settings.allow_keyless:
        providers[Network.ETHEREUM] = BlockscoutProvider(client)
    else:
        raise ConfigError(
            "no ETHERSCAN_API_KEY configured while ALLOW_KEYLESS=false; "
            "Ethereum needs a key in that mode"
        )

    bsc_key = settings.bsc_activity_key
    providers[Network.BSC] = BscProvider(
        client,
        settings.bsc_rpc_url,
        EtherscanV2Provider(
            client,
            network=Network.BSC,
            chain_id=ETHERSCAN_CHAIN_IDS[Network.BSC],
            api_key=bsc_key,
        )
        if bsc_key
        else None,
    )

    return providers


def log_provider_summaries(providers: dict[Network, Provider]) -> None:
    """Flush one-per-batch provider warnings (currently the BSC breaker)."""

    for provider in providers.values():
        if isinstance(provider, BscProvider):
            provider.log_activity_summary()


def describe_modes(providers: dict[Network, Provider]) -> dict[Network, str]:
    descriptions: dict[Network, str] = {}
    for network, provider in providers.items():
        if isinstance(provider, EtherscanV2Provider):
            descriptions[network] = f"Etherscan V2 chainid={provider._chain_id}"
        elif isinstance(provider, BlockscoutProvider):
            descriptions[network] = "Blockscout (keyless)"
        elif isinstance(provider, BscProvider):
            descriptions[network] = (
                "public RPC balance + Etherscan V2 activity"
                if provider._activity is not None
                else "public RPC balance only (activity unverifiable)"
            )
        elif isinstance(provider, BitcoinProvider):
            descriptions[network] = provider.source_label
        elif isinstance(provider, SolanaProvider):
            descriptions[network] = f"Solana JSON-RPC ({provider._endpoint})"
        else:  # pragma: no cover - defensive
            descriptions[network] = type(provider).__name__
    return descriptions
