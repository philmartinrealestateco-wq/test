"""USD price feed with a primary source, a fallback, and an on-disk cache."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .net import HttpClient, ProviderError

LOG = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
TOKEN_PRICE_URLS = {
    "binance-smart-chain": "https://api.coingecko.com/api/v3/simple/token_price/binance-smart-chain",
    "ethereum": "https://api.coingecko.com/api/v3/simple/token_price/ethereum",
}
TOKEN_PRICE_URL = TOKEN_PRICE_URLS["binance-smart-chain"]
COINBASE_URL = "https://api.coinbase.com/v2/prices/{pair}/spot"

#: The public CoinGecko tier rejects more than one contract address per call
#: (error_code 10012, measured 2026-11-11), so requests are issued singly.
#: Raise this only with a paid plan.
TOKEN_PRICE_BATCH = 1

#: CoinGecko also answers 400 for throttling-adjacent failures and for a single
#: unsupported contract, so 400 is retried for token prices only. A bad contract
#: still ends up as "missing" rather than failing the whole run.
TOKEN_PRICE_RETRY_STATUSES = (400,)

COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "SOL": "solana",
    "XRP": "ripple",
}
ASSETS = tuple(COINGECKO_IDS)


class PriceError(RuntimeError):
    """No usable price could be obtained; callers must not guess a valuation."""


@dataclass(frozen=True)
class PriceSnapshot:
    prices: dict[str, Decimal]
    source: str
    fetched_at: datetime
    updated_at: datetime | None = None
    stale: bool = False

    def usd(self, asset: str) -> Decimal | None:
        return self.prices.get(asset)

    def describe_age(self, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        minutes = int((now - self.fetched_at).total_seconds() // 60)
        if minutes < 1:
            return "less than a minute old"
        if minutes < 60:
            return f"{minutes} min old"
        return f"{minutes // 60} h old"


@dataclass(frozen=True)
class TokenPriceSnapshot:
    """USD prices keyed by lowercased BEP-20 contract address."""

    prices: dict[str, Decimal]
    source: str
    fetched_at: datetime | None
    missing: set[str]

    def usd(self, contract: str) -> Decimal | None:
        return self.prices.get(contract.lower())


class PriceService:
    """Fetches all four native-asset prices in one shot and caches them."""

    def __init__(
        self,
        client: HttpClient,
        *,
        ttl_seconds: int = 300,
        cache_path: Path | None = None,
        token_cache_path: Path | None = None,
    ) -> None:
        self._client = client
        self._ttl = timedelta(seconds=ttl_seconds)
        self._cache_path = cache_path
        self._token_cache_path = token_cache_path
        # Process-lifetime cache: a batch must not re-price a token just because
        # its TTL elapsed halfway through the run.
        self._token_memory: dict[str, tuple[Decimal, datetime]] = {}

    def get(self, *, force_refresh: bool = False) -> PriceSnapshot:
        cached = None if force_refresh else self._read_cache()
        if cached is not None and not cached.stale:
            LOG.info("using cached prices (%s, %s)", cached.source, cached.describe_age())
            return cached

        snapshot: PriceSnapshot | None = None
        errors: list[str] = []

        try:
            snapshot = self._from_coingecko()
        except ProviderError as exc:
            errors.append(f"coingecko: {exc}")
            LOG.warning("CoinGecko price lookup failed: %s", exc)

        if snapshot is None:
            try:
                snapshot = self._from_coinbase()
            except ProviderError as exc:
                errors.append(f"coinbase: {exc}")
                LOG.warning("Coinbase price lookup failed: %s", exc)

        if snapshot is None:
            fallback = self._read_cache(allow_expired=True)
            if fallback is not None:
                LOG.warning("all price sources failed; reusing cached prices")
                return replace(fallback, stale=True)
            raise PriceError("no price source reachable: " + "; ".join(errors))

        self._write_cache(snapshot)
        return snapshot

    # -- sources ---------------------------------------------------------

    def _from_coingecko(self) -> PriceSnapshot:
        payload = self._client.get_json(
            COINGECKO_URL,
            {
                "ids": ",".join(COINGECKO_IDS.values()),
                "vs_currencies": "usd",
                "include_last_updated_at": "true",
            },
            host="api.coingecko.com",
        )
        if not isinstance(payload, dict):
            raise ProviderError("unexpected CoinGecko payload shape", host="api.coingecko.com")

        prices: dict[str, Decimal] = {}
        latest_update: datetime | None = None
        for asset, identifier in COINGECKO_IDS.items():
            entry = payload.get(identifier)
            if not isinstance(entry, dict) or "usd" not in entry:
                raise ProviderError(
                    f"CoinGecko response is missing {identifier}", host="api.coingecko.com"
                )
            prices[asset] = _to_decimal(entry["usd"], f"CoinGecko {identifier}")
            stamp = entry.get("last_updated_at")
            if isinstance(stamp, (int, float)):
                updated = datetime.fromtimestamp(stamp, tz=timezone.utc)
                latest_update = updated if latest_update is None else max(latest_update, updated)

        return PriceSnapshot(
            prices=prices,
            source="CoinGecko simple/price",
            fetched_at=datetime.now(timezone.utc),
            updated_at=latest_update,
        )

    def _from_coinbase(self) -> PriceSnapshot:
        prices: dict[str, Decimal] = {}
        for asset in ASSETS:
            payload = self._client.get_json(
                COINBASE_URL.format(pair=f"{asset}-USD"), host="api.coinbase.com"
            )
            try:
                amount = payload["data"]["amount"]
            except (KeyError, TypeError) as exc:
                raise ProviderError(
                    f"unexpected Coinbase payload for {asset}", host="api.coinbase.com"
                ) from exc
            prices[asset] = _to_decimal(amount, f"Coinbase {asset}")

        now = datetime.now(timezone.utc)
        return PriceSnapshot(
            prices=prices,
            source="Coinbase spot prices",
            fetched_at=now,
            updated_at=now,
        )

    # -- token prices ----------------------------------------------------

    def get_token_prices(
        self,
        contracts: list[str],
        *,
        platform: str = "binance-smart-chain",
        force_refresh: bool = False,
    ) -> TokenPriceSnapshot:
        """Price EVM token contracts one call per contract, with a TTL cache.

        ``platform`` selects CoinGecko's asset platform (``ethereum`` or
        ``binance-smart-chain``). Never raises for a provider failure: an
        unpriced contract lands in ``missing`` so the caller can mark the
        wallet's valuation incomplete instead of guessing.
        """

        wanted = [contract.lower() for contract in contracts]
        if not wanted:
            return TokenPriceSnapshot({}, "not requested", None, set())

        if force_refresh:
            self._token_memory.clear()

        now = datetime.now(timezone.utc)
        prices: dict[str, Decimal] = {}

        # 1. Anything already priced in this process is final for the batch.
        for contract in wanted:
            remembered = self._token_memory.get(contract)
            if remembered is not None:
                prices[contract] = remembered[0]

        # 2. Fall back to the on-disk cache, which does respect the TTL.
        cached = self._read_disk_token_cache() if len(prices) < len(wanted) else {}
        for contract in wanted:
            if contract in prices:
                continue
            entry = cached.get(contract)
            if entry is not None and now - entry[1] <= self._ttl:
                prices[contract] = entry[0]
                self._token_memory[contract] = entry

        source = "cache" if prices else ""
        missing = [contract for contract in wanted if contract not in prices]
        if missing:
            fetched, failed = self._fetch_token_prices(missing, platform)
            if fetched:
                prices.update(fetched)
                self._write_token_cache(fetched)
                source = f"CoinGecko token_price ({platform})"
            if failed:
                LOG.warning(
                    "token price unavailable for %s of %s contract(s) after retries",
                    len(failed),
                    len(missing),
                )

        unresolved = {contract for contract in wanted if contract not in prices}
        if not prices:
            return TokenPriceSnapshot({}, "unavailable", None, unresolved)
        return TokenPriceSnapshot(prices, source or "cache", now, unresolved)

    def _fetch_token_prices(
        self, contracts: list[str], platform: str
    ) -> tuple[dict[str, Decimal], set[str]]:
        """Fetch independently so one failing contract does not sink the rest."""

        url = TOKEN_PRICE_URLS.get(platform)
        if url is None:
            raise ValueError(f"unknown token price platform {platform!r}")

        prices: dict[str, Decimal] = {}
        failed: set[str] = set()

        for start in range(0, len(contracts), TOKEN_PRICE_BATCH):
            chunk = contracts[start : start + TOKEN_PRICE_BATCH]
            try:
                payload = self._client.get_json(
                    url,
                    {"contract_addresses": ",".join(chunk), "vs_currencies": "usd"},
                    host="api.coingecko.com",
                    retry_statuses=TOKEN_PRICE_RETRY_STATUSES,
                )
            except ProviderError as exc:
                # 429/400 already went through the transport's backoff; give up
                # on this contract only and keep pricing the rest.
                LOG.debug("token price for %s failed: %s", chunk[0][:10], exc)
                failed.update(chunk)
                continue

            if not isinstance(payload, dict):
                LOG.debug("unexpected token price payload for %s", chunk[0][:10])
                failed.update(chunk)
                continue

            for contract, entry in payload.items():
                if isinstance(entry, dict) and "usd" in entry:
                    try:
                        prices[str(contract).lower()] = _to_decimal(
                            entry["usd"], f"CoinGecko {contract}"
                        )
                    except ProviderError as exc:
                        # A nonsense price is treated like a missing one; the
                        # chunk check below records the contract as failed.
                        LOG.debug("rejected token price for %s: %s", str(contract)[:10], exc)

            for contract in chunk:
                if contract not in prices:
                    failed.add(contract)

        return prices, failed

    def _read_disk_token_cache(self) -> dict[str, tuple[Decimal, datetime]]:
        if self._token_cache_path is None or not self._token_cache_path.exists():
            return {}
        try:
            raw = json.loads(self._token_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

        entries: dict[str, tuple[Decimal, datetime]] = {}
        for contract, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            try:
                entries[str(contract).lower()] = (
                    Decimal(str(entry["usd"])),
                    datetime.fromisoformat(entry["fetched_at"]),
                )
            except (KeyError, ValueError, InvalidOperation):
                continue
        return entries

    def _write_token_cache(self, prices: dict[str, Decimal]) -> None:
        stamp = datetime.now(timezone.utc)
        for contract, price in prices.items():
            self._token_memory[contract] = (price, stamp)

        if self._token_cache_path is None:
            return
        existing = self._read_disk_token_cache()
        existing.update(self._token_memory)
        try:
            self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_cache_path.write_text(
                json.dumps(
                    {
                        contract: {"usd": str(price), "fetched_at": fetched_at.isoformat()}
                        for contract, (price, fetched_at) in existing.items()
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            LOG.debug("could not write token price cache: %s", exc)

    # -- cache -----------------------------------------------------------

    def _read_cache(self, *, allow_expired: bool = False) -> PriceSnapshot | None:
        if self._cache_path is None or not self._cache_path.exists():
            return None
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(raw["fetched_at"])
            updated_raw = raw.get("updated_at")
            updated_at = datetime.fromisoformat(updated_raw) if updated_raw else None
            prices = {asset: Decimal(str(value)) for asset, value in raw["prices"].items()}
        except (OSError, ValueError, KeyError, InvalidOperation) as exc:
            LOG.debug("ignoring unreadable price cache: %s", exc)
            return None

        if set(prices) != set(ASSETS):
            return None
        source = str(raw.get("source", "cache"))
        if allow_expired:
            return PriceSnapshot(prices, source, fetched_at, updated_at, stale=True)

        age = datetime.now(timezone.utc) - fetched_at
        if age > self._ttl:
            return None
        return PriceSnapshot(prices, source, fetched_at, updated_at)

    def _write_cache(self, snapshot: PriceSnapshot) -> None:
        if self._cache_path is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(
                    {
                        "fetched_at": snapshot.fetched_at.isoformat(),
                        "updated_at": snapshot.updated_at.isoformat() if snapshot.updated_at else None,
                        "source": snapshot.source,
                        "prices": {asset: str(value) for asset, value in snapshot.prices.items()},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            LOG.debug("could not write price cache: %s", exc)


def _to_decimal(value: object, origin: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ProviderError(f"{origin} returned a non-numeric price {value!r}") from exc
    if parsed <= 0:
        raise ProviderError(f"{origin} returned a non-positive price {value!r}")
    return parsed
