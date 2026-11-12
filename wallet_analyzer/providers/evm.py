"""EVM providers: Etherscan V2, Blockscout, and BSC via public JSON-RPC."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from ..models import Network
from ..net import HttpClient, ProviderError
from .base import JsonRpcProvider, ProviderResult

LOG = logging.getLogger(__name__)

ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
BLOCKSCOUT_ETH_URL = "https://eth.blockscout.com/api"
WEI_PER_ETHER = Decimal(10) ** 18


def _wei_to_native(raw: object, source: str) -> Decimal:
    try:
        return Decimal(str(raw)) / WEI_PER_ETHER
    except (InvalidOperation, TypeError) as exc:
        raise ProviderError(f"{source} returned a non-numeric balance {raw!r}") from exc


def _tx_timestamp(transactions: list[dict]) -> datetime | None:
    if not transactions:
        return None
    timestamp = transactions[0].get("timeStamp")
    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


class _ExplorerProvider:
    """Shared Etherscan-style JSON handling for Etherscan V2 and Blockscout."""

    host = ""
    balance_source = ""
    activity_source = ""

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    def _request(self, params: dict[str, object]) -> object:
        payload = self._client.get_json(self._url(), self._query(params), host=self.host)
        if not isinstance(payload, dict):
            raise ProviderError(f"{self.host} returned an unexpected payload", host=self.host)

        result = payload.get("result")
        if isinstance(result, list):
            return result
        if str(payload.get("status")) == "1":
            return result
        if self._is_empty(result, payload.get("message")):
            return []
        raise ProviderError(
            f"{self.balance_source}: {result or payload.get('message') or 'unknown error'}",
            host=self.host,
        )

    @staticmethod
    def _is_empty(result: object, message: object) -> bool:
        text = f"{message or ''} {result if isinstance(result, str) else ''}".lower()
        return "no transactions found" in text or "no records found" in text

    def fetch_activity(self, address: str) -> tuple[datetime | None, str]:
        """Return the most recent native transaction time (``None`` if there is none)."""

        transactions = self._request(
            {
                "module": "account",
                "action": "txlist",
                "address": address,
                "page": 1,
                "offset": 1,
                "sort": "desc",
            }
        )
        if not isinstance(transactions, list):
            raise ProviderError(f"{self.host} returned an unexpected transaction list", host=self.host)
        return _tx_timestamp(transactions), ""

    def _url(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _query(self, params: dict[str, object]) -> dict[str, object]:  # pragma: no cover
        raise NotImplementedError


class EtherscanV2Provider(_ExplorerProvider):
    """Etherscan's unified V2 API.

    A free key serves Ethereum (chainid=1). Non-Ethereum chains such as BNB
    Smart Chain (chainid=56) return an explicit "upgrade your plan" error, which
    is surfaced rather than hidden.
    """

    host = "api.etherscan.io"

    def __init__(self, client: HttpClient, *, network: Network, chain_id: int, api_key: str) -> None:
        super().__init__(client)
        self.network = network
        self._chain_id = chain_id
        self._api_key = api_key
        self.balance_source = f"Etherscan V2 (chainid={chain_id})"
        self.activity_source = f"Etherscan V2 (chainid={chain_id})"

    def _url(self) -> str:
        return ETHERSCAN_V2_URL

    def _query(self, params: dict[str, object]) -> dict[str, object]:
        return {"chainid": self._chain_id, "apikey": self._api_key, **params}

    def fetch(self, address: str) -> ProviderResult:
        raw_balance = self._request(
            {"module": "account", "action": "balance", "address": address, "tag": "latest"}
        )
        balance = _wei_to_native(raw_balance, self.balance_source)
        last_tx_at, note = self.fetch_activity(address)

        notes = ["Native coin only; ERC-20/BEP-20 token holdings are excluded"]
        if note:
            notes.append(note)

        return ProviderResult(
            balance=balance,
            balance_verified=True,
            last_tx_at=last_tx_at,
            activity_verified=True,
            balance_source=self.balance_source,
            activity_source=self.activity_source,
            notes=notes,
        )


class BlockscoutProvider(_ExplorerProvider):
    """Keyless Ethereum fallback: Blockscout exposes an Etherscan-compatible API."""

    host = "eth.blockscout.com"

    def __init__(self, client: HttpClient) -> None:
        super().__init__(client)
        self.network = Network.ETHEREUM
        self.balance_source = "Blockscout (eth.blockscout.com)"
        self.activity_source = "Blockscout (eth.blockscout.com)"

    def _url(self) -> str:
        return BLOCKSCOUT_ETH_URL

    def _query(self, params: dict[str, object]) -> dict[str, object]:
        return params

    def fetch(self, address: str) -> ProviderResult:
        raw_balance = self._request(
            {"module": "account", "action": "balance", "address": address, "tag": "latest"}
        )
        balance = _wei_to_native(raw_balance, self.balance_source)
        last_tx_at, note = self.fetch_activity(address)

        return ProviderResult(
            balance=balance,
            balance_verified=True,
            last_tx_at=last_tx_at,
            activity_verified=True,
            balance_source=self.balance_source,
            activity_source=self.activity_source,
            notes=[
                "Native ETH only; ERC-20 token holdings are excluded",
                *([note] if note else []),
            ],
        )


class BscProvider:
    """BNB Smart Chain.

    Balance always comes from public JSON-RPC, with the configured endpoint tried
    first and the documented public dataseeds behind it. Activity needs an
    indexed history API, which on BSC means an Etherscan V2 plan that covers
    chainid=56; without one the wallet is reported as PARTIAL instead of being
    assumed inactive.

    Coverage is deliberately narrow and stated per result: this provider verifies
    the native BNB balance only. It does not verify BEP-20 token balances, and
    without the paid history API it does not verify activity either.
    """

    network = Network.BSC

    #: Verified mainnet dataseeds (chainId 0x38), used in order after the
    #: configured endpoint. All are read-only public nodes.
    PUBLIC_FALLBACKS = (
        "https://bsc-dataseed.binance.org",
        "https://bsc-dataseed1.defibit.io",
        "https://bsc-dataseed1.ninicoin.io",
    )

    def __init__(
        self,
        client: HttpClient,
        rpc_url: str,
        activity_provider: EtherscanV2Provider | None = None,
        *,
        fallback_urls: tuple[str, ...] | None = None,
    ) -> None:
        candidates = [rpc_url, *(fallback_urls if fallback_urls is not None else self.PUBLIC_FALLBACKS)]
        self._endpoints: list[JsonRpcProvider] = []
        for url in candidates:
            if not url or any(provider.endpoint == _host_of(url) for provider in self._endpoints):
                continue
            self._endpoints.append(
                JsonRpcProvider(
                    client,
                    url,
                    host=_host_of(url),
                    env_hint="BSC_RPC_URL",
                )
            )
        self._activity = activity_provider

    def _read_balance(self, address: str) -> tuple[Decimal, str]:
        """Return ``(balance, endpoint label)`` from the first healthy node."""

        failures: list[str] = []
        for rpc in self._endpoints:
            try:
                raw_balance = rpc.call("eth_getBalance", [address, "latest"])
                wei = int(str(raw_balance), 16)
            except ProviderError as exc:
                failures.append(str(exc))
                continue
            except (TypeError, ValueError):
                failures.append(f"{rpc.endpoint} returned an unreadable balance")
                continue
            return Decimal(wei) / WEI_PER_ETHER, rpc.endpoint

        raise ProviderError(
            "no BSC RPC endpoint answered: " + ("; ".join(failures) or "none configured")
        )

    def fetch(self, address: str) -> ProviderResult:
        balance, endpoint = self._read_balance(address)

        notes = [
            "Native BNB balance only; transaction history is not covered by this RPC"
        ]
        last_tx_at: datetime | None = None
        activity_verified = False
        activity_source = ""

        if self._activity is None:
            notes.append(
                "BNB Smart Chain activity requires an Etherscan V2 key with chainid=56 "
                "coverage; native balance verified via public RPC"
            )
        else:
            try:
                last_tx_at, note = self._activity.fetch_activity(address)
                activity_verified = True
                activity_source = self._activity.activity_source
                if note:
                    notes.append(note)
            except ProviderError as exc:
                LOG.warning("BSC activity lookup failed: %s", exc)
                notes.append(f"activity lookup failed: {exc}")

        return ProviderResult(
            balance=balance,
            balance_verified=True,
            last_tx_at=last_tx_at,
            activity_verified=activity_verified,
            balance_source=f"BSC JSON-RPC ({endpoint})",
            activity_source=activity_source,
            notes=notes,
        )


def _host_of(url: str) -> str:
    return urlparse(url).hostname or "invalid-endpoint"
