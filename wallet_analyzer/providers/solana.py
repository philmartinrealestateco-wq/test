"""Solana balances and activity over plain JSON-RPC."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

from ..models import Network
from ..net import HttpClient, ProviderError
from .base import JsonRpcProvider, ProviderResult

LAMPORTS_PER_SOL = Decimal(10) ** 9


class SolanaProvider:
    network = Network.SOLANA

    #: How many recent signatures to inspect for a successful transaction.
    SIGNATURE_LOOKBACK = 25

    def __init__(self, client: HttpClient, rpc_url: str) -> None:
        host = urlparse(rpc_url).hostname or "invalid-endpoint"
        self._rpc = JsonRpcProvider(
            client, rpc_url, host=host, env_hint="SOLANA_RPC_URL"
        )
        self._endpoint = self._rpc.endpoint

    def fetch(self, address: str) -> ProviderResult:
        balance_result = self._rpc.call(
            "getBalance", [address, {"commitment": "finalized"}]
        )
        if not isinstance(balance_result, dict) or "value" not in balance_result:
            raise ProviderError("Solana RPC returned an unexpected getBalance payload")
        try:
            lamports = int(balance_result["value"])
        except (TypeError, ValueError) as exc:
            raise ProviderError("Solana RPC returned a non-integer lamport balance") from exc

        signatures = self._rpc.call(
            "getSignaturesForAddress",
            [address, {"limit": self.SIGNATURE_LOOKBACK, "commitment": "finalized"}],
        )
        if not isinstance(signatures, list):
            raise ProviderError("Solana RPC returned an unexpected signature list")

        successful = [entry for entry in signatures if entry.get("err") is None]
        last_tx_at: datetime | None = None
        activity_verified = True
        notes = ["Native SOL only; SPL token holdings are excluded"]

        if successful:
            block_time = successful[0].get("blockTime")
            if isinstance(block_time, int):
                last_tx_at = datetime.fromtimestamp(block_time, tz=timezone.utc)
            else:
                activity_verified = False
                notes.append("Solana RPC returned a signature without blockTime")
        elif len(signatures) >= self.SIGNATURE_LOOKBACK:
            # Every inspected signature failed; older history was not requested.
            activity_verified = False
            notes.append(
                f"the newest {len(signatures)} signatures all failed; older activity was not checked"
            )
        elif signatures:
            notes.append("all returned signatures are failed transactions")

        return ProviderResult(
            balance=Decimal(lamports) / LAMPORTS_PER_SOL,
            balance_verified=True,
            last_tx_at=last_tx_at,
            activity_verified=activity_verified,
            balance_source=f"Solana JSON-RPC getBalance ({self._endpoint})",
            activity_source=f"Solana JSON-RPC getSignaturesForAddress ({self._endpoint})",
            notes=notes,
        )
