"""BEP-20 ABI encoding and balance retrieval over a stubbed JSON-RPC client."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wallet_analyzer.bep20 import (
    BALANCE_OF,
    DECIMALS,
    SYMBOL,
    Bep20Provider,
    decode_decimals,
    decode_symbol,
    decode_uint,
    encode_address,
    encode_call,
)
from wallet_analyzer.net import ProviderError
from wallet_analyzer.tokens import TokenSpec

WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
TOKEN_A = "0x55d398326f99059fF775485246999027B3197955"
TOKEN_B = "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82"


def word(value: int) -> str:
    return f"{value:064x}"


def abi_string(text: str) -> str:
    raw = text.encode()
    return "0x" + word(32) + word(len(raw)) + raw.hex().ljust(64, "0")


class FakeClient:
    """Answers eth_call by inspecting the call data, like a tiny node would."""

    def __init__(self, balances=None, *, batch_supported=True, fail_contracts=()):
        self.balances = balances or {}
        self.batch_supported = batch_supported
        self.fail_contracts = set(fail_contracts)
        self.batch_calls: list[list[dict]] = []
        self.single_calls: list[dict] = []

    def post_json(self, url, payload, host=None):
        if isinstance(payload, list):
            self.batch_calls.append(payload)
            if not self.batch_supported:
                return {"jsonrpc": "2.0", "id": None, "error": {"message": "batch not supported"}}
            return [self._answer(item) for item in payload]
        self.single_calls.append(payload)
        return self._answer(payload)

    def _answer(self, item: dict) -> dict:
        params = item["params"][0]
        data = params.get("data", "")
        contract = params.get("to", "").lower()

        if contract in {c.lower() for c in self.fail_contracts}:
            return {"jsonrpc": "2.0", "id": item["id"], "error": {"message": "execution reverted"}}

        if data.startswith(DECIMALS):
            return {"jsonrpc": "2.0", "id": item["id"], "result": "0x" + word(18)}
        if data.startswith(SYMBOL):
            name = "TKA" if contract == TOKEN_A.lower() else "TKB"
            return {"jsonrpc": "2.0", "id": item["id"], "result": abi_string(name)}
        if data.startswith(BALANCE_OF):
            raw = self.balances.get(contract, 0)
            return {"jsonrpc": "2.0", "id": item["id"], "result": "0x" + word(raw)}
        return {"jsonrpc": "2.0", "id": item["id"], "result": "0x"}


def provider(client: FakeClient, tokens: list[TokenSpec]) -> Bep20Provider:
    return Bep20Provider(client, "https://bsc-dataseed.binance.org", tokens)


# -- encoding ------------------------------------------------------------


def test_selectors_match_the_erc20_standard() -> None:
    assert BALANCE_OF == "0x70a08231"
    assert DECIMALS == "0x313ce567"
    assert SYMBOL == "0x95d89b41"


def test_encode_address_pads_to_one_word() -> None:
    encoded = encode_address(WALLET)
    assert len(encoded) == 64
    assert encoded == "0" * 24 + WALLET.lower().removeprefix("0x")


def test_encode_call_concatenates_words() -> None:
    call = encode_call(BALANCE_OF, encode_address(WALLET))
    assert call == BALANCE_OF + encode_address(WALLET)


def test_encode_address_rejects_wrong_length() -> None:
    with pytest.raises(ValueError):
        encode_address("0x1234")


# -- decoding ------------------------------------------------------------


def test_decode_uint_and_decimals() -> None:
    assert decode_uint("0x" + word(18)) == 18
    assert decode_uint("0x") is None
    assert decode_decimals("0x" + word(18)) == 18
    assert decode_decimals("0x" + word(99)) is None
    assert decode_decimals("0x" + word(37)) is None


def test_decode_symbol_from_abi_string() -> None:
    assert decode_symbol(abi_string("USDT")) == "USDT"


def test_decode_symbol_from_legacy_bytes32() -> None:
    legacy = "0x" + b"MKR".ljust(32, b"\x00").hex()
    assert decode_symbol(legacy) == "MKR"


def test_decode_symbol_garbage_is_none() -> None:
    assert decode_symbol("0x") is None
    assert decode_symbol("0xzz") is None


# -- balances ------------------------------------------------------------


def test_scan_reads_balances_and_metadata() -> None:
    client = FakeClient({TOKEN_A.lower(): 5 * 10**18, TOKEN_B.lower(): 2 * 10**18})
    scan = provider(client, [TokenSpec(TOKEN_A), TokenSpec(TOKEN_B)]).scan(WALLET)

    assert scan.complete
    assert scan.checked == 2
    amounts = {holding.contract.lower(): holding.amount for holding in scan.holdings}
    assert amounts[TOKEN_A.lower()] == Decimal(5)
    assert amounts[TOKEN_B.lower()] == Decimal(2)
    symbols = {holding.contract.lower(): holding.symbol for holding in scan.holdings}
    assert symbols[TOKEN_A.lower()] == "TKA"


def test_scan_keeps_zero_balances_for_the_caller_to_filter() -> None:
    client = FakeClient({TOKEN_A.lower(): 0})
    scan = provider(client, [TokenSpec(TOKEN_A)]).scan(WALLET)

    assert len(scan.holdings) == 1
    assert scan.holdings[0].raw_balance == 0
    assert scan.holdings[0].amount == Decimal(0)


def test_metadata_is_cached_between_wallets() -> None:
    client = FakeClient({TOKEN_A.lower(): 1})
    bep20 = provider(client, [TokenSpec(TOKEN_A)])

    bep20.scan(WALLET)
    metadata_calls_after_first = sum(
        1
        for batch in client.batch_calls
        for item in batch
        if item["params"][0].get("data", "").startswith((DECIMALS, SYMBOL))
    )
    bep20.scan(WALLET)
    metadata_calls_after_second = sum(
        1
        for batch in client.batch_calls
        for item in batch
        if item["params"][0].get("data", "").startswith((DECIMALS, SYMBOL))
    )

    assert metadata_calls_after_first == 2  # decimals + symbol for one token
    assert metadata_calls_after_second == metadata_calls_after_first


def test_failed_contract_marks_the_scan_incomplete() -> None:
    client = FakeClient({TOKEN_A.lower(): 7}, fail_contracts=[TOKEN_B])
    scan = provider(client, [TokenSpec(TOKEN_A), TokenSpec(TOKEN_B)]).scan(WALLET)

    assert not scan.complete
    assert TOKEN_B in scan.failed
    assert len(scan.holdings) == 1
    assert "could not be read" in scan.note


def test_falls_back_to_single_calls_when_batching_is_unsupported() -> None:
    client = FakeClient({TOKEN_A.lower(): 3 * 10**18}, batch_supported=False)
    scan = provider(client, [TokenSpec(TOKEN_A)]).scan(WALLET)

    assert scan.complete
    assert scan.holdings[0].amount == Decimal(3)
    assert client.single_calls  # batched request was rejected, singles were used


def test_scan_without_tokens_returns_empty() -> None:
    scan = provider(FakeClient(), []).scan(WALLET)
    assert scan.holdings == []
    assert scan.complete


def test_scan_propagates_endpoint_configuration_errors() -> None:
    client = FakeClient()
    bep20 = Bep20Provider(client, "bare-key-not-a-url", [TokenSpec(TOKEN_A)])
    with pytest.raises(ProviderError, match="BSC_RPC_URL"):
        bep20.scan(WALLET)


def test_ethereum_endpoint_hint_is_surfaced() -> None:
    client = FakeClient()
    bep20 = Bep20Provider(
        client,
        "bare-key-not-a-url",
        [TokenSpec(TOKEN_A)],
        env_hint="ETHEREUM_RPC_URL",
    )
    with pytest.raises(ProviderError, match="ETHEREUM_RPC_URL"):
        bep20.scan(WALLET)


def test_decimals_are_resolved_per_contract_not_assumed() -> None:
    """BUSD/USDC report 18 on-chain; another token reports 6. Scaling must follow."""

    six_decimals_token = "0x" + "2" * 40
    facts = {
        TOKEN_A.lower(): (18, "BUSD", 2 * 10**18),
        six_decimals_token.lower(): (6, "SIX", 5 * 10**6),
    }

    class Client:
        def post_json(self, url, payload, host=None):
            items = payload if isinstance(payload, list) else [payload]
            answers = []
            for item in items:
                contract = item["params"][0]["to"].lower()
                data = item["params"][0]["data"]
                decimals, symbol, balance = facts[contract]
                if data.startswith(DECIMALS):
                    result = "0x" + word(decimals)
                elif data.startswith(SYMBOL):
                    result = abi_string(symbol)
                else:
                    result = "0x" + word(balance)
                answers.append({"jsonrpc": "2.0", "id": item["id"], "result": result})
            return answers if isinstance(payload, list) else answers[0]

    scan = Bep20Provider(
        Client(),
        "https://bsc-dataseed.binance.org",
        [TokenSpec(TOKEN_A), TokenSpec(six_decimals_token)],
    ).scan(WALLET)

    amounts = {holding.contract.lower(): holding.amount for holding in scan.holdings}
    assert amounts[TOKEN_A.lower()] == Decimal(2)
    assert amounts[six_decimals_token.lower()] == Decimal(5)
    assert {holding.decimals for holding in scan.holdings} == {18, 6}
