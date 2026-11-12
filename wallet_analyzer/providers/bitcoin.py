"""Bitcoin balances and activity via the Blockchain.com Explorer API.

``rawaddr`` returns the aggregate satoshi balance, the lifetime transaction
count and the newest transactions in a single keyless call, which is why no
second request is needed here.
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

    def fetch(self, address: str) -> ProviderResult:
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
            raise ProviderError(f"blockchain.com: {payload['error']}", host=self.HOST)

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
