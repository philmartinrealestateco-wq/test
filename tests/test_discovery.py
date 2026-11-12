"""Holder discovery: Etherscan payload parsing, CSV fallback, and file writing."""

from __future__ import annotations

from pathlib import Path

import pytest

from wallet_analyzer.discovery import (
    CHAIN_IDS,
    DiscoveryError,
    discover_holders,
    extract_addresses,
    fetch_token_holders,
    normalize_address,
    read_holders_csv,
    write_holders,
)
from wallet_analyzer.net import ProviderError

USDT = "0x55d398326f99059fF775485246999027B3197955"
HOLDER_A = "0x28C6c06298d514Db089934071355E5743bf21d60"
HOLDER_B_LOWER = "0x1fbe2acee135d991592f167ac371f3dd893a508b"


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def get_json(self, url, params=None, host=None, retry_statuses=()):
        self.calls.append(params or {})
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def holder_payload(*addresses: str) -> dict:
    return {
        "status": "1",
        "message": "OK",
        "result": [
            {"TokenHolderAddress": address, "TokenHolderQuantity": "1000"}
            for address in addresses
        ],
    }


# -- Etherscan parsing ---------------------------------------------------


def test_parses_documented_holder_payload() -> None:
    client = FakeClient(holder_payload(HOLDER_A, HOLDER_B_LOWER))

    holders = fetch_token_holders(client, "TESTKEY", USDT, chain_id=56)

    assert holders == [HOLDER_A, normalize_address(HOLDER_B_LOWER)]
    sent = client.calls[0]
    assert sent["chainid"] == 56
    assert sent["module"] == "token"
    assert sent["action"] == "tokenholderlist"
    assert sent["contractaddress"] == USDT
    assert sent["apikey"] == "TESTKEY"


def test_api_pro_requirement_is_surfaced() -> None:
    client = FakeClient(
        {
            "status": "0",
            "message": "NOTOK",
            "result": "Sorry, it looks like you are trying to access an API Pro endpoint.",
        }
    )

    with pytest.raises(ProviderError, match="API Pro"):
        fetch_token_holders(client, "TESTKEY", USDT, chain_id=1)


def test_chain_plan_restriction_is_surfaced() -> None:
    client = FakeClient(
        {
            "status": "0",
            "message": "NOTOK",
            "result": "Free API access is not supported for this chain.",
        }
    )

    with pytest.raises(ProviderError, match="not supported for this chain"):
        fetch_token_holders(client, "TESTKEY", USDT, chain_id=56)


def test_non_list_result_is_rejected() -> None:
    client = FakeClient({"status": "1", "message": "OK", "result": "nope"})

    with pytest.raises(ProviderError, match="no holder array"):
        fetch_token_holders(client, "TESTKEY", USDT, chain_id=1)


def test_payload_without_usable_addresses_is_rejected() -> None:
    client = FakeClient(
        {"status": "1", "message": "OK", "result": [{"TokenHolderQuantity": "1"}]}
    )

    with pytest.raises(ProviderError, match="no usable holder addresses"):
        fetch_token_holders(client, "TESTKEY", USDT, chain_id=1)


def test_alternate_address_key_is_accepted() -> None:
    client = FakeClient(
        {"status": "1", "message": "OK", "result": [{"address": HOLDER_A, "amount": "5"}]}
    )

    assert fetch_token_holders(client, "TESTKEY", USDT, chain_id=56) == [HOLDER_A]


# -- address normalization ----------------------------------------------


def test_normalize_accepts_lowercase_and_returns_checksum() -> None:
    normalized = normalize_address(HOLDER_B_LOWER)

    assert normalized is not None
    assert normalized != HOLDER_B_LOWER
    assert normalized.lower() == HOLDER_B_LOWER


@pytest.mark.parametrize(
    "value",
    ["", "0x1234", "not-an-address", "0x" + "z" * 40, HOLDER_A + "00"],
)
def test_normalize_rejects_non_addresses(value: str) -> None:
    assert normalize_address(value) is None


def test_extract_addresses_dedupes_case_insensitively() -> None:
    extracted = extract_addresses([HOLDER_A, HOLDER_A.lower(), "header", HOLDER_B_LOWER])

    assert extracted == [HOLDER_A, normalize_address(HOLDER_B_LOWER)]


# -- CSV fallback --------------------------------------------------------


def write_csv(tmp_path: Path, body: str, name: str = "holders.csv") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_csv_reads_first_column_and_skips_header(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        f"address,balance\n{HOLDER_A},1000\n{HOLDER_B_LOWER},2000\n",
    )

    holders = read_holders_csv(path)

    assert holders == [HOLDER_A, normalize_address(HOLDER_B_LOWER)]


def test_csv_skips_notes_blanks_and_short_values(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "\n".join(
            [
                "# exported from dune",
                "",
                f"{HOLDER_A},1",
                "0x1234",
                "total,3",
            ]
        ),
    )

    assert read_holders_csv(path) == [HOLDER_A]


def test_csv_without_addresses_raises(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "address,balance\nfoo,1\n")

    with pytest.raises(DiscoveryError, match="no wallet addresses"):
        read_holders_csv(path)


def test_missing_csv_raises(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryError, match="not found"):
        read_holders_csv(tmp_path / "nope.csv")


# -- orchestration -------------------------------------------------------


def test_api_success_uses_the_api_source() -> None:
    addresses, source = discover_holders(lambda: [HOLDER_A])

    assert addresses == [HOLDER_A]
    assert source == "Etherscan V2 tokenholderlist"


def test_api_failure_falls_back_to_csv(tmp_path: Path) -> None:
    path = write_csv(tmp_path, f"address\n{HOLDER_A}\n")

    def failing() -> list[str]:
        raise ProviderError("Sorry, it looks like you are trying to access an API Pro endpoint.")

    addresses, source = discover_holders(failing, csv_path=path)

    assert addresses == [HOLDER_A]
    assert source == f"CSV fallback ({path.name})"


def test_api_failure_without_csv_explains_the_plan_limit() -> None:
    def failing() -> list[str]:
        raise ProviderError("Sorry, it looks like you are trying to access an API Pro endpoint.")

    with pytest.raises(DiscoveryError) as excinfo:
        discover_holders(failing)

    message = str(excinfo.value)
    assert "API Pro" in message
    assert "--csv" in message


# -- output file ---------------------------------------------------------


def test_write_holders_one_address_per_line(tmp_path: Path) -> None:
    path = tmp_path / "holders.txt"

    count = write_holders(path, [HOLDER_A, HOLDER_B_LOWER])

    assert count == 2
    assert path.read_text(encoding="utf-8") == f"{HOLDER_A}\n{HOLDER_B_LOWER}\n"


def test_write_creates_the_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "out" / "holders.txt"
    write_holders(path, [HOLDER_A])
    assert path.exists()


# -- CLI wiring ----------------------------------------------------------


def test_chain_ids_cover_the_supported_networks() -> None:
    assert CHAIN_IDS["bsc"] == 56
    assert CHAIN_IDS["ethereum"] == 1


def test_cli_rejects_a_bad_contract_without_network(capsys) -> None:
    from wallet_analyzer.cli import main

    assert main(["discover", "--token", "not-an-address"]) == 2
    assert "20-byte contract address" in capsys.readouterr().err


def test_cli_rejects_an_unknown_chain(capsys) -> None:
    from wallet_analyzer.cli import main

    assert main(["discover", "--token", USDT, "--chain", "solana"]) == 2
    assert "unknown chain" in capsys.readouterr().err


def test_cli_rejects_an_out_of_range_limit(capsys) -> None:
    from wallet_analyzer.cli import main

    assert main(["discover", "--token", USDT, "--limit", "0"]) == 2
    assert "--limit" in capsys.readouterr().err
