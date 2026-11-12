"""Token registry parsing, merging and CLI argument handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from wallet_analyzer.tokens import (
    DEFAULT_BEP20_TOKENS,
    TokenListError,
    TokenSpec,
    load_token_list,
    merge_tokens,
    parse_token_argument,
)

USDT = "0x55d398326f99059fF775485246999027B3197955"
BUSD = "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56"
USDC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
FDUSD = "0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409"
DAI = "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3"


def test_default_registry_is_unique_and_evm_shaped() -> None:
    keys = [token.key for token in DEFAULT_BEP20_TOKENS]
    assert len(keys) == len(set(keys))
    assert all(key.startswith("0x") and len(key) == 42 for key in keys)


def test_default_registry_pins_stablecoins() -> None:
    by_contract = {token.key: token for token in DEFAULT_BEP20_TOKENS}
    expected = {BUSD: "BUSD", USDC: "USDC", FDUSD: "FDUSD", DAI: "DAI"}

    for contract, symbol in expected.items():
        assert by_contract[contract.lower()].symbol == symbol
        assert by_contract[contract.lower()].contract == contract

    # 13 unique contracts: these four plus USDT, WBNB, CAKE, BTCB, ETH, LINK,
    # DOT, ADA and XRP.
    assert len(DEFAULT_BEP20_TOKENS) == 13
    assert len(by_contract) == 13


def test_re_adding_existing_contracts_does_not_duplicate_the_registry() -> None:
    merged = merge_tokens(
        DEFAULT_BEP20_TOKENS,
        [
            TokenSpec(BUSD, "BUSD"),
            TokenSpec(USDC.lower(), "USDC"),
            TokenSpec(FDUSD, "FDUSD"),
            TokenSpec(DAI, "DAI"),
        ],
    )

    assert len(merged) == len(DEFAULT_BEP20_TOKENS) == 13
    assert [token.key for token in merged].count(FDUSD.lower()) == 1


def test_merge_keeps_first_definition_and_dedupes_case_insensitively() -> None:
    merged = merge_tokens(
        [TokenSpec(USDT, "USDT")],
        [TokenSpec(USDT.lower(), "OTHER"), TokenSpec("0x" + "1" * 40, "X")],
    )
    assert len(merged) == 2
    assert merged[0].symbol == "USDT"


def test_parse_token_argument_with_symbol_and_decimals() -> None:
    token = parse_token_argument(f"{USDT}:USDT:18")
    assert token.contract == USDT
    assert token.symbol == "USDT"
    assert token.decimals == 18


def test_parse_token_argument_bare_contract() -> None:
    token = parse_token_argument(USDT)
    assert token.symbol == ""
    assert token.decimals is None


@pytest.mark.parametrize("value", ["0x1234", "not-an-address", USDT + ":USDT:99", USDT + ":T:x"])
def test_parse_token_argument_rejects_bad_input(value: str) -> None:
    with pytest.raises(TokenListError):
        parse_token_argument(value)


def test_load_token_list_skips_header_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "tokens.csv"
    path.write_text(
        "contract,symbol,decimals\n"
        f"# a comment line\n"
        f"{USDT},USDT,18\n"
        "0x0000000000000000000000000000000000000001,,6\n",
        encoding="utf-8",
    )
    tokens = load_token_list(path)

    assert [token.symbol for token in tokens] == ["USDT", ""]
    assert tokens[1].decimals == 6


def test_load_token_list_requires_contracts(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("contract,symbol\n", encoding="utf-8")
    with pytest.raises(TokenListError):
        load_token_list(path)


def test_load_token_list_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TokenListError, match="not found"):
        load_token_list(tmp_path / "nope.csv")
