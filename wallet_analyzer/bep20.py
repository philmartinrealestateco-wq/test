"""BEP-20 balances over public BNB Chain JSON-RPC.

Only two ERC-20-compatible calls are used: ``balanceOf(address)`` on the token
contract, plus ``decimals()`` and ``symbol()`` once per contract. All three are
plain ``eth_call`` reads, so nothing is signed or broadcast.

What this cannot do: discover which tokens a wallet holds. That needs a log
indexer, and the public dataseed rejects ``eth_getLogs`` outright. The caller
supplies the token allowlist; anything outside it is invisible here.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

from .crypto import keccak256
from .models import TokenHolding
from .net import HttpClient, ProviderError
from .providers.base import JsonRpcProvider
from .tokens import TokenSpec

LOG = logging.getLogger(__name__)


def _selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode("ascii"))[:4].hex()


BALANCE_OF = _selector("balanceOf(address)")
DECIMALS = _selector("decimals()")
SYMBOL = _selector("symbol()")


def encode_address(address: str) -> str:
    """ABI-encode an address as one 32-byte word (no 0x)."""

    body = address.lower().removeprefix("0x")
    if len(body) != 40:
        raise ValueError(f"not a 20-byte hex address: {address!r}")
    return body.rjust(64, "0")


def encode_call(signature_selector: str, *words: str) -> str:
    return signature_selector + "".join(words)


def decode_uint(payload: str | None) -> int | None:
    if not payload or not isinstance(payload, str):
        return None
    text = payload.removeprefix("0x")
    if not text:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def decode_decimals(payload: str | None) -> int | None:
    value = decode_uint(payload)
    if value is None or not 0 <= value <= 36:
        return None
    return value


def decode_symbol(payload: str | None) -> str | None:
    """Handle both ABI ``string`` and legacy ``bytes32`` symbol returns."""

    if not payload or not isinstance(payload, str):
        return None
    text = payload.removeprefix("0x")
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return None
    if len(raw) < 32:
        return None

    if len(raw) >= 64:
        offset = int.from_bytes(raw[0:32], "big")
        length = int.from_bytes(raw[32:64], "big")
        if offset == 32 and 0 < length <= 64 and len(raw) >= 64 + length:
            decoded = raw[64 : 64 + length].decode("utf-8", errors="replace")
            return decoded.strip("\x00").strip() or None

    legacy = raw[:32].rstrip(b"\x00").decode("utf-8", errors="replace").strip()
    return legacy or None


@dataclass
class TokenScan:
    """Outcome of checking one wallet against the configured token allowlist."""

    holdings: list[TokenHolding]
    checked: int
    failed: list[str]
    complete: bool
    note: str = ""


class Bep20Provider:
    def __init__(
        self,
        client: HttpClient,
        rpc_url: str,
        tokens: list[TokenSpec],
        *,
        batch_size: int = 40,
    ) -> None:
        self._rpc = JsonRpcProvider(
            client,
            rpc_url,
            host=urlparse(rpc_url).hostname or "invalid-endpoint",
            env_hint="BSC_RPC_URL",
        )
        self._tokens = list(tokens)
        self._batch_size = max(1, batch_size)
        self._metadata: dict[str, tuple[str | None, int | None]] = {}
        self._metadata_lock = threading.Lock()
        self._metadata_ready = False

    @property
    def contracts(self) -> list[str]:
        return [token.contract for token in self._tokens]

    @property
    def endpoint(self) -> str:
        return self._rpc.endpoint

    # -- metadata --------------------------------------------------------

    def _ensure_metadata(self) -> None:
        with self._metadata_lock:
            if self._metadata_ready:
                return
            pending = [t for t in self._tokens if t.key not in self._metadata]
            if pending:
                calls: list[tuple[str, list]] = []
                for token in pending:
                    calls.append(("eth_call", [{"to": token.contract, "data": DECIMALS}, "latest"]))
                    calls.append(("eth_call", [{"to": token.contract, "data": SYMBOL}, "latest"]))

                results = self._call_many_safe(calls)
                for index, token in enumerate(pending):
                    decimals = _ok_value(results[index * 2])
                    symbol = _ok_value(results[index * 2 + 1])
                    self._metadata[token.key] = (
                        decode_symbol(symbol),
                        decode_decimals(decimals),
                    )
            self._metadata_ready = True

    def _call_many_safe(self, calls: list[tuple[str, list]]) -> list[tuple[bool, object]]:
        """Batch when the node allows it, otherwise fall back to single calls."""

        try:
            return self._rpc.call_many(calls)
        except ProviderError as exc:
            LOG.debug("batch eth_call unsupported (%s); falling back to single calls", exc)
        return [self._rpc.call_one(method, params) for method, params in calls]

    # -- balances --------------------------------------------------------

    def scan(self, address: str) -> TokenScan:
        if not self._tokens:
            return TokenScan(holdings=[], checked=0, failed=[], complete=True)

        self._ensure_metadata()

        encoded = encode_address(address)
        holdings: list[TokenHolding] = []
        failed: list[str] = []

        for chunk_start in range(0, len(self._tokens), self._batch_size):
            chunk = self._tokens[chunk_start : chunk_start + self._batch_size]
            calls = [
                (
                    "eth_call",
                    [{"to": token.contract, "data": encode_call(BALANCE_OF, encoded)}, "latest"],
                )
                for token in chunk
            ]
            results = self._call_many_safe(calls)

            for token, (ok, value) in zip(chunk, results):
                if not ok:
                    failed.append(token.contract)
                    continue
                raw = decode_uint(value if isinstance(value, str) else None)
                if raw is None:
                    failed.append(token.contract)
                    continue

                symbol, decimals = self._metadata.get(token.key, (None, None))
                symbol = symbol or token.symbol or _short(token.contract)
                decimals = decimals if decimals is not None else token.decimals
                if decimals is None:
                    failed.append(token.contract)
                    continue

                holdings.append(
                    TokenHolding(
                        contract=token.contract,
                        symbol=symbol,
                        decimals=decimals,
                        raw_balance=raw,
                        amount=Decimal(raw) / (Decimal(10) ** decimals),
                    )
                )

        note = ""
        if failed:
            note = f"{len(failed)} of {len(self._tokens)} token contracts could not be read"
        return TokenScan(
            holdings=holdings,
            checked=len(self._tokens),
            failed=failed,
            complete=not failed,
            note=note,
        )


def _ok_value(entry: tuple[bool, object]) -> str | None:
    ok, value = entry
    return value if ok and isinstance(value, str) else None


def _short(address: str) -> str:
    return f"{address[:6]}..{address[-4:]}"
