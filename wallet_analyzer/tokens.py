"""Token registry for BEP-20 contracts.

This list is an *allowlist*: the tool checks tokens it has been told about. It
cannot discover the full token set of a wallet, because that needs a log indexer
which the public BNB Chain RPC does not provide.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TokenSpec:
    """A token to check. ``symbol``/``decimals`` are hints, not trusted values."""

    contract: str
    symbol: str = ""
    decimals: int | None = None

    @property
    def key(self) -> str:
        return self.contract.lower()


#: Contract addresses are mainnet BSC. Decimals are resolved on-chain; the hints
#: here only matter if a contract's metadata call reverts.
DEFAULT_BEP20_TOKENS: tuple[TokenSpec, ...] = (
    TokenSpec("0x55d398326f99059fF775485246999027B3197955", "USDT"),
    TokenSpec("0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", "USDC"),
    TokenSpec("0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56", "BUSD"),
    TokenSpec("0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409", "FDUSD"),
    TokenSpec("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "WBNB"),
    TokenSpec("0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82", "CAKE"),
    TokenSpec("0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3", "DAI"),
    TokenSpec("0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c", "BTCB"),
    TokenSpec("0x2170Ed0880ac9A755fd29B2688956BD959F933F8", "ETH"),
    TokenSpec("0xF8A0BF9cF54Bb92F17374d9e9A321E6a111a51bD", "LINK"),
    TokenSpec("0x7083609fCE4d1d8Dc0C979AAb8c869Ea2C873402", "DOT"),
    TokenSpec("0x3EE2200Efb3400fAbB9AacF31297cBdD1d435D47", "ADA"),
    TokenSpec("0x1D2F0da169ceB9fC7B3144628dB156f3F6c60dBE", "XRP"),
)


class TokenListError(RuntimeError):
    """A token file could not be read or contains no usable contracts."""


def merge_tokens(*groups: tuple[TokenSpec, ...] | list[TokenSpec]) -> list[TokenSpec]:
    """Union token specs, keeping the first definition of each contract."""

    merged: list[TokenSpec] = []
    seen: set[str] = set()
    for group in groups:
        for token in group:
            if token.key in seen:
                continue
            merged.append(token)
            seen.add(token.key)
    return merged


def parse_token_argument(value: str) -> TokenSpec:
    """Parse ``0xCONTRACT[:SYMBOL[:DECIMALS]]`` from the command line."""

    parts = [part.strip() for part in value.split(":")]
    contract = parts[0]
    if not contract.startswith("0x") or len(contract) != 42:
        raise TokenListError(f"token contract must be 0x + 40 hex characters: {contract!r}")
    symbol = parts[1] if len(parts) > 1 else ""
    decimals: int | None = None
    if len(parts) > 2 and parts[2]:
        try:
            decimals = int(parts[2])
        except ValueError as exc:
            raise TokenListError(f"token decimals must be an integer: {parts[2]!r}") from exc
        if not 0 <= decimals <= 36:
            raise TokenListError("token decimals must be between 0 and 36")
    return TokenSpec(contract, symbol, decimals)


def load_token_list(path: Path) -> list[TokenSpec]:
    """Read a token CSV with ``contract[,symbol[,decimals]]`` per row."""

    if not path.exists():
        raise TokenListError(f"token file not found: {path}")

    tokens: list[TokenSpec] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        for line_number, row in enumerate(reader, start=1):
            if not row or not row[0].strip() or row[0].strip().startswith("#"):
                continue
            cells = [cell.strip() for cell in row]
            if cells[0].lower() in {"contract", "address", "token"}:
                continue  # header row
            try:
                tokens.append(
                    TokenSpec(
                        contract=cells[0],
                        symbol=cells[1] if len(cells) > 1 else "",
                        decimals=int(cells[2]) if len(cells) > 2 and cells[2] else None,
                    )
                )
            except ValueError as exc:
                raise TokenListError(f"{path.name} line {line_number}: {exc}") from exc

    if not tokens:
        raise TokenListError(f"{path.name} contains no token contracts")
    return tokens
