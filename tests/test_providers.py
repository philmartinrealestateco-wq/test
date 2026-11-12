"""Provider payload handling with a stubbed HTTP client (no network)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from wallet_analyzer.models import Network
from wallet_analyzer.net import ProviderError
from wallet_analyzer.providers.bitcoin import BitcoinProvider
from wallet_analyzer.providers.evm import BscProvider, BlockscoutProvider, EtherscanV2Provider
from wallet_analyzer.providers.solana import SolanaProvider

BTC = "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97"
EVM = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
SOL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


class FakeClient:
    def __init__(self, get_handler=None, post_handler=None):
        self.get_handler = get_handler
        self.post_handler = post_handler
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []

    def get_json(self, url, params=None, host=None):
        self.get_calls.append({"url": url, "params": params, "host": host})
        return self.get_handler(url, params)

    def post_json(self, url, payload, host=None):
        self.post_calls.append({"url": url, "payload": payload, "host": host})
        return self.post_handler(payload)


# -- Bitcoin -------------------------------------------------------------


def test_bitcoin_parses_balance_and_latest_transaction() -> None:
    client = FakeClient(
        get_handler=lambda url, params: {
            "final_balance": 15701008336931,
            "n_tx": 356,
            "txs": [{"time": 1794379036}],
        }
    )
    result = BitcoinProvider(client).fetch(BTC)

    assert result.balance == Decimal("157010.08336931")
    assert result.balance_verified
    assert result.activity_verified
    assert result.tx_count == 356
    assert result.last_tx_at == datetime.fromtimestamp(1794379036, tz=timezone.utc)


def test_bitcoin_without_transactions_is_verified_inactive() -> None:
    client = FakeClient(
        get_handler=lambda url, params: {"final_balance": 0, "n_tx": 0, "txs": []}
    )
    result = BitcoinProvider(client).fetch(BTC)

    assert result.balance == Decimal(0)
    assert result.last_tx_at is None
    assert result.activity_verified


def test_bitcoin_error_payload_raises() -> None:
    client = FakeClient(get_handler=lambda url, params: {"error": "bad address"})
    with pytest.raises(ProviderError, match="bad address"):
        BitcoinProvider(client).fetch(BTC)


def test_bitcoin_requests_only_one_transaction() -> None:
    client = FakeClient(get_handler=lambda url, params: {"final_balance": 1, "n_tx": 1, "txs": [{"time": 1}]})
    BitcoinProvider(client).fetch(BTC)
    assert client.get_calls[0]["params"] == {"limit": 1}


# -- Etherscan V2 --------------------------------------------------------


def _etherscan_handler(balance: str = "5794616987480413546", tx_response=None):
    def handler(url, params):
        if params["action"] == "balance":
            return {"status": "1", "message": "OK", "result": balance}
        if tx_response is not None:
            return tx_response
        return {"status": "1", "message": "OK", "result": [{"timeStamp": "1794311939"}]}

    return handler


def test_etherscan_reads_balance_and_activity() -> None:
    client = FakeClient(get_handler=_etherscan_handler())
    provider = EtherscanV2Provider(
        client, network=Network.ETHEREUM, chain_id=1, api_key="TESTKEY123456"
    )
    result = provider.fetch(EVM)

    assert result.balance == Decimal("5794616987480413546") / Decimal(10**18)
    assert result.activity_verified
    assert result.last_tx_at is not None
    assert result.balance_source == "Etherscan V2 (chainid=1)"


def test_etherscan_sends_chain_and_key() -> None:
    client = FakeClient(get_handler=_etherscan_handler())
    provider = EtherscanV2Provider(
        client, network=Network.BSC, chain_id=56, api_key="TESTKEY123456"
    )
    provider.fetch(EVM)
    sent = client.get_calls[0]["params"]
    assert sent["chainid"] == 56
    assert sent["apikey"] == "TESTKEY123456"
    assert client.get_calls[0]["host"] == "api.etherscan.io"


def test_etherscan_plan_error_is_surfaced() -> None:
    client = FakeClient(
        get_handler=lambda url, params: {
            "status": "0",
            "message": "NOTOK",
            "result": "Free API access is not supported for this chain. "
            "Please upgrade your api plan for full chain coverage.",
        }
    )
    provider = EtherscanV2Provider(
        client, network=Network.BSC, chain_id=56, api_key="TESTKEY123456"
    )
    with pytest.raises(ProviderError, match="upgrade your api plan"):
        provider.fetch(EVM)


def test_etherscan_empty_transaction_list_means_inactive() -> None:
    client = FakeClient(
        get_handler=_etherscan_handler(
            tx_response={"status": "0", "message": "No transactions found", "result": []}
        )
    )
    result = EtherscanV2Provider(
        client, network=Network.ETHEREUM, chain_id=1, api_key="TESTKEY123456"
    ).fetch(EVM)
    assert result.last_tx_at is None
    assert result.activity_verified


# -- Blockscout ----------------------------------------------------------


def test_blockscout_works_without_a_key() -> None:
    def handler(url, params):
        if params["action"] == "balance":
            return {"status": "1", "message": "OK", "result": "1000000000000000000"}
        return {"status": "1", "message": "OK", "result": [{"timeStamp": "1794311939"}]}

    provider = BlockscoutProvider(FakeClient(get_handler=handler))
    result = provider.fetch(EVM)

    assert result.balance == Decimal(1)
    assert result.activity_verified
    assert "Blockscout" in result.balance_source


def test_blockscout_handles_no_transactions() -> None:
    def handler(url, params):
        if params["action"] == "balance":
            return {"status": "1", "message": "OK", "result": "0"}
        return {"status": "0", "message": "No transactions found", "result": []}

    result = BlockscoutProvider(FakeClient(get_handler=handler)).fetch(EVM)
    assert result.balance == Decimal(0)
    assert result.last_tx_at is None


# -- BSC -----------------------------------------------------------------


def test_bsc_without_activity_provider_is_partial() -> None:
    client = FakeClient(post_handler=lambda payload: {"jsonrpc": "2.0", "id": 1, "result": "0x1bc16d674ec80000"})
    result = BscProvider(client, "https://bsc-dataseed.binance.org").fetch(EVM)

    assert result.balance == Decimal(2)
    assert result.balance_verified
    assert not result.activity_verified
    assert "Etherscan V2" in " ".join(result.notes)


def test_bsc_uses_activity_provider_when_available() -> None:
    rpc = FakeClient(post_handler=lambda payload: {"jsonrpc": "2.0", "id": 1, "result": "0xde0b6b3a7640000"})
    activity = EtherscanV2Provider(
        FakeClient(get_handler=_etherscan_handler()),
        network=Network.BSC,
        chain_id=56,
        api_key="TESTKEY123456",
    )
    result = BscProvider(rpc, "https://bsc-dataseed.binance.org", activity).fetch(EVM)

    assert result.balance_verified and result.activity_verified
    assert result.activity_source == "Etherscan V2 (chainid=56)"


def test_bsc_falls_back_to_the_next_public_endpoint() -> None:
    attempts: list[str] = []

    def handler(payload):
        attempts.append(payload["method"])
        if len(attempts) == 1:
            raise ProviderError("primary node down")
        return {"jsonrpc": "2.0", "id": 1, "result": "0xde0b6b3a7640000"}

    provider = BscProvider(
        FakeClient(post_handler=handler),
        "https://primary.invalid.example",
        fallback_urls=("https://secondary.example",),
    )
    result = provider.fetch(EVM)

    assert result.balance == Decimal(1)
    assert "secondary.example" in result.balance_source
    assert len(attempts) == 2


def test_bsc_reports_when_every_endpoint_fails() -> None:
    def always_down(payload):
        raise ProviderError("node unreachable")

    provider = BscProvider(
        FakeClient(post_handler=always_down),
        "https://a.example",
        fallback_urls=("https://b.example",),
    )
    with pytest.raises(ProviderError, match="no BSC RPC endpoint answered"):
        provider.fetch(EVM)


def test_bsc_never_claims_token_or_history_coverage() -> None:
    client = FakeClient(post_handler=lambda payload: {"jsonrpc": "2.0", "id": 1, "result": "0x0"})
    result = BscProvider(client, "https://bsc-dataseed.binance.org", None).fetch(EVM)

    notes = " ".join(result.notes).lower()
    assert "native bnb balance only" in notes
    assert "transaction history is not covered" in notes
    assert result.activity_verified is False


def test_bsc_degrades_when_activity_provider_fails() -> None:
    rpc = FakeClient(post_handler=lambda payload: {"jsonrpc": "2.0", "id": 1, "result": "0xde0b6b3a7640000"})
    failing = EtherscanV2Provider(
        FakeClient(
            get_handler=lambda url, params: {
                "status": "0",
                "message": "NOTOK",
                "result": "Free API access is not supported for this chain.",
            }
        ),
        network=Network.BSC,
        chain_id=56,
        api_key="TESTKEY123456",
    )
    result = BscProvider(rpc, "https://bsc-dataseed.binance.org", failing).fetch(EVM)

    assert result.balance_verified
    assert not result.activity_verified
    assert "activity lookup failed" in " ".join(result.notes)


# -- Solana --------------------------------------------------------------


def _solana_client(signatures, *, lamports: int | None = 9300003246657371):
    def handler(payload):
        if payload["method"] == "getBalance":
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"context": {"slot": 1}, "value": lamports},
            }
        if payload["method"] == "getSignaturesForAddress":
            return {"jsonrpc": "2.0", "id": 1, "result": signatures}
        raise AssertionError(f"unexpected method {payload['method']}")

    return FakeClient(post_handler=handler)


def test_solana_reads_balance_and_newest_successful_signature() -> None:
    client = _solana_client(
        [
            {"signature": "x", "blockTime": 1794412726, "err": None},
            {"signature": "y", "blockTime": 1794303026, "err": {"InstructionError": []}},
        ]
    )
    result = SolanaProvider(client, "https://api.mainnet-beta.solana.com").fetch(SOL)

    assert result.balance == Decimal("9300003.246657371")
    assert result.last_tx_at == datetime.fromtimestamp(1794412726, tz=timezone.utc)
    assert result.activity_verified


def test_solana_all_failed_signatures_are_not_activity() -> None:
    client = _solana_client(
        [{"signature": "x", "blockTime": 1794412726, "err": {"InstructionError": []}}]
    )
    result = SolanaProvider(client, "https://api.mainnet-beta.solana.com").fetch(SOL)

    assert result.last_tx_at is None
    assert result.activity_verified
    assert "failed transactions" in " ".join(result.notes)


def test_solana_rpc_error_is_raised() -> None:
    client = FakeClient(
        post_handler=lambda payload: {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid param"}}
    )
    with pytest.raises(ProviderError, match="invalid param"):
        SolanaProvider(client, "https://api.mainnet-beta.solana.com").fetch(SOL)


def test_solana_rejects_a_bare_key_instead_of_a_url() -> None:
    token = "eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6ImFAYi5jb20ifQ.d00f_MY_ok"
    client = FakeClient(post_handler=lambda payload: pytest.fail("no request should be made"))

    with pytest.raises(ProviderError) as excinfo:
        SolanaProvider(client, token).fetch(SOL)

    message = str(excinfo.value)
    assert "SOLANA_RPC_URL" in message
    assert "d00f_MY_ok" not in message
    assert client.post_calls == []


# -- Solana: zero balance and malformed responses ------------------------


def test_solana_zero_balance_is_verified_not_an_error() -> None:
    client = _solana_client([], lamports=0)

    result = SolanaProvider(client, "https://api.mainnet-beta.solana.com").fetch(SOL)

    assert result.balance == Decimal(0)
    assert result.balance_verified
    assert result.activity_verified
    assert result.last_tx_at is None


def test_solana_zero_balance_with_history_keeps_the_timestamp() -> None:
    client = _solana_client(
        [{"signature": "x", "blockTime": 1794412726, "err": None}], lamports=0
    )

    result = SolanaProvider(client, "https://api.mainnet-beta.solana.com").fetch(SOL)

    assert result.balance == Decimal(0)
    assert result.last_tx_at == datetime.fromtimestamp(1794412726, tz=timezone.utc)


def test_solana_null_balance_value_is_rejected() -> None:
    client = _solana_client([], lamports=None)

    with pytest.raises(ProviderError, match="non-integer lamport balance"):
        SolanaProvider(client, "https://api.mainnet-beta.solana.com").fetch(SOL)


def test_solana_missing_balance_value_is_rejected() -> None:
    client = FakeClient(
        post_handler=lambda payload: {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"context": {"slot": 1}},
        }
    )

    with pytest.raises(ProviderError, match="unexpected getBalance payload"):
        SolanaProvider(client, "https://api.mainnet-beta.solana.com").fetch(SOL)


def test_solana_non_list_signatures_are_rejected() -> None:
    def handler(payload):
        if payload["method"] == "getBalance":
            return {"jsonrpc": "2.0", "id": 1, "result": {"context": {}, "value": 0}}
        return {"jsonrpc": "2.0", "id": 1, "result": {"not": "a list"}}

    with pytest.raises(ProviderError, match="unexpected signature list"):
        SolanaProvider(
            FakeClient(post_handler=handler), "https://api.mainnet-beta.solana.com"
        ).fetch(SOL)


def test_solana_null_signature_result_is_rejected() -> None:
    def handler(payload):
        if payload["method"] == "getBalance":
            return {"jsonrpc": "2.0", "id": 1, "result": {"context": {}, "value": 0}}
        return {"jsonrpc": "2.0", "id": 1, "result": None}

    with pytest.raises(ProviderError, match="unexpected signature list"):
        SolanaProvider(
            FakeClient(post_handler=handler), "https://api.mainnet-beta.solana.com"
        ).fetch(SOL)


def test_solana_signature_without_blocktime_is_not_activity() -> None:
    client = _solana_client([{"signature": "x", "err": None}])

    result = SolanaProvider(client, "https://api.mainnet-beta.solana.com").fetch(SOL)

    assert result.last_tx_at is None
    assert result.activity_verified is False
    assert "without blockTime" in " ".join(result.notes)


def test_bsc_rejects_a_malformed_rpc_url() -> None:
    client = FakeClient(post_handler=lambda payload: pytest.fail("no request should be made"))
    with pytest.raises(ProviderError, match="BSC_RPC_URL"):
        BscProvider(client, "not-a-url", fallback_urls=()).fetch(EVM)
