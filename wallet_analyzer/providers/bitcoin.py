"""Bitcoin balances and activity via the Blockchain.com Explorer API.

``rawaddr`` returns the aggregate satoshi balance, the lifetime transaction
count and the newest transactions in a single keyless call, which is why no
second request is needed here.

Blockchain.com bans shared IPs for a day at a time (HTTP 429 with a
``Retry-After`` measured in hours), so when that call fails the provider falls
back to blockstream.info's keyless Esplora address API. The fallback needs two
calls: the address document carries the aggregate satoshis, and ``/txs``
carries the timestamps.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from ..models import Network
from ..net import HttpClient, ProviderError
from .base import ProviderResult

SATOSHIS_PER_BTC = Decimal(10) ** 8


class BitcoinProvider:
    network = Network.BITCOIN
    BASE_URL = "https://blockchain.info/rawaddr/{address}"
    HOST = "blockchain.info"
    FALLBACK_BASE = "https://blockstream.info/api/address/{address}"
    FALLBACK_HOST = "blockstream.info"

    def __init__(self, client: HttpClient, api_key: str | None = None) -> None:
        self._client = client
        self._api_key = api_key

    @property
    def source_label(self) -> str:
        return (
            "blockchain.com rawaddr API (keyed)"
            if self._api_key
            else "blockchain.com rawaddr API (keyless)"
        )

    #: Statuses worth asking another host about; an address-level rejection is not.
    AVAILABILITY_STATUSES = {408, 425, 429, 500, 502, 503, 504}

    def fetch(self, address: str) -> ProviderResult:
        try:
            return self._fetch_blockchain(address)
        except ProviderError as exc:
            if exc.status is not None and exc.status not in self.AVAILABILITY_STATUSES:
                raise
            return self._fetch_mempool(address)

    def _fetch_blockchain(self, address: str) -> ProviderResult:
        params: dict[str, object] = {"limit": 1}
        if self._api_key:
            # Blockchain.com's optional Query API credential. The endpoint
            # answers identically when it is absent, so keyless stays default.
            params["api_code"] = self._api_key

        payload = self._client.get_json(
            self.BASE_URL.format(address=address),
            params,
            host=self.HOST,
        )
        if not isinstance(payload, dict):
            raise ProviderError("blockchain.com returned an unexpected payload", host=self.HOST)
        if payload.get("error"):
            raise ProviderError(
                f"blockchain.com: {payload['error']}", host=self.HOST, status=400
            )

        try:
            satoshis = int(payload["final_balance"])
            tx_count = int(payload.get("n_tx", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("blockchain.com payload is missing balance fields", host=self.HOST) from exc

        balance = Decimal(satoshis) / SATOSHIS_PER_BTC

        last_tx_at: datetime | None = None
        notes: list[str] = []
        transactions = payload.get("txs") or []

        if tx_count == 0:
            activity_verified = True
        elif transactions:
            timestamp = transactions[0].get("time")
            if isinstance(timestamp, (int, float)):
                last_tx_at = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
                activity_verified = True
            else:
                activity_verified = False
                notes.append("blockchain.com returned a transaction without a timestamp")
        else:
            activity_verified = False
            notes.append("blockchain.com reported transactions but returned none")

        notes.append("UTXO chain: balance is the sum of unspent outputs, not a token balance")

        return ProviderResult(
            balance=balance,
            balance_verified=True,
            last_tx_at=last_tx_at,
            tx_count=tx_count,
            activity_verified=activity_verified,
            balance_source=self.source_label,
            activity_source=self.source_label,
            notes=notes,
        )

    def _fetch_mempool(self, address: str) -> ProviderResult:
        base = self.FALLBACK_BASE.format(address=address)
        payload = self._client.get_json(base, host=self.FALLBACK_HOST)
        if not isinstance(payload, dict):
            raise ProviderError(
                "blockstream.info returned an unexpected payload",
                host=self.FALLBACK_HOST,
            )

        chain = payload.get("chain_stats") or {}
        mempool = payload.get("mempool_stats") or {}
        try:
            funded = int(chain.get("funded_txo_sum", 0)) + int(
                mempool.get("funded_txo_sum", 0)
            )
            spent = int(chain.get("spent_txo_sum", 0)) + int(
                mempool.get("spent_txo_sum", 0)
            )
            tx_count = int(chain.get("tx_count", 0)) + int(
                mempool.get("tx_count", 0)
            )
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                "blockstream.info payload is missing balance fields",
                host=self.FALLBACK_HOST,
            ) from exc

        source = f"blockstream.info address API ({self.FALLBACK_HOST})"
        notes = [
            "UTXO chain: balance is the sum of unspent outputs, not a token balance",
            "served by blockstream.info after blockchain.com was unavailable",
        ]

        last_tx_at: datetime | None = None
        activity_verified = True
        if tx_count > 0:
            transactions = self._client.get_json(
                f"{base}/txs", host=self.FALLBACK_HOST
            )
            if not isinstance(transactions, list) or not transactions:
                activity_verified = False
                notes.append("blockstream.info reported transactions but returned none")
            else:
                status = transactions[0].get("status") or {}
                block_time = status.get("block_time")
                if isinstance(block_time, (int, float)):
                    last_tx_at = datetime.fromtimestamp(
                        int(block_time), tz=timezone.utc
                    )
                elif status.get("confirmed") is False:
                    last_tx_at = datetime.now(timezone.utc)
                else:
                    activity_verified = False
                    notes.append(
                        "blockstream.info returned a transaction without a timestamp"
                    )

        return ProviderResult(
            balance=Decimal(funded - spent) / SATOSHIS_PER_BTC,
            balance_verified=True,
            last_tx_at=last_tx_at,
            tx_count=tx_count,
            activity_verified=activity_verified,
            balance_source=source,
            activity_source=source,
            notes=notes,
        )
