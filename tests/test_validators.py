"""Address validation and network detection."""

from __future__ import annotations

import pytest

from wallet_analyzer.models import Network
from wallet_analyzer.validators import detect_networks, validate

VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
BURN = "1FeexV6bAHb8ybZjqQMjJrcCrHGW9sb6uF"
P2SH = "3C8mzzeiyakvXkQadUM5f98zCt5NQWwXo1"
BECH32 = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
SOLANA = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
XRP = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"


@pytest.mark.parametrize("address", [BURN, P2SH, BECH32, BECH32.upper()])
def test_bitcoin_accepts_valid_formats(address: str) -> None:
    assert validate(Network.BITCOIN, address).valid


def test_bitcoin_rejects_bad_base58_checksum() -> None:
    outcome = validate(Network.BITCOIN, "1FeexV6bAHb8ybZjqQMjJrcCrHGW9sb6uG")
    assert not outcome.valid
    assert "checksum" in outcome.reason


def test_bitcoin_rejects_bech32_with_broken_checksum() -> None:
    assert not validate(Network.BITCOIN, "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5").valid


def test_bitcoin_rejects_ethereum_address() -> None:
    assert not validate(Network.BITCOIN, VITALIK).valid


def test_evm_accepts_lowercase_and_normalizes() -> None:
    outcome = validate(Network.ETHEREUM, VITALIK.lower())
    assert outcome.valid
    assert outcome.normalized == VITALIK


def test_evm_rejects_bad_checksum() -> None:
    # Valid EIP-55 form ends in an uppercase D; lowercasing it breaks the checksum.
    broken = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD"
    outcome = validate(Network.ETHEREUM, broken)
    assert not outcome.valid
    assert "EIP-55" in outcome.reason


def test_evm_rejects_short_address() -> None:
    assert not validate(Network.BSC, "0x1234").valid


def test_solana_accepts_public_key() -> None:
    assert validate(Network.SOLANA, SOLANA).valid


@pytest.mark.parametrize(
    "address",
    [
        "1" * 44,  # decodes to 44 zero bytes, not a 32-byte key
        "0OIl" + "1" * 30,  # forbidden base58 characters
        VITALIK,
    ],
)
def test_solana_rejects_wrong_shapes(address: str) -> None:
    assert not validate(Network.SOLANA, address).valid


def test_xrp_accepts_a_classic_account() -> None:
    assert validate(Network.XRP, XRP).valid


def test_xrp_rejects_a_bad_checksum() -> None:
    outcome = validate(Network.XRP, XRP[:-1] + "U")
    assert not outcome.valid
    assert "checksum" in outcome.reason


def test_xrp_rejects_an_x_address() -> None:
    outcome = validate(Network.XRP, "X" + "1" * 46)
    assert not outcome.valid
    assert "X-address" in outcome.reason


def test_detect_xrp() -> None:
    assert detect_networks(XRP) == [Network.XRP]


def test_empty_address_is_invalid() -> None:
    outcome = validate(Network.BITCOIN, "   ")
    assert not outcome.valid
    assert "empty" in outcome.reason


def test_detect_expands_evm_to_both_chains() -> None:
    assert detect_networks(VITALIK) == [Network.ETHEREUM, Network.BSC]


def test_detect_bitcoin_and_solana() -> None:
    assert detect_networks(BECH32) == [Network.BITCOIN]
    assert detect_networks(BURN) == [Network.BITCOIN]
    assert detect_networks(SOLANA) == [Network.SOLANA]


def test_detect_rejects_garbage() -> None:
    assert detect_networks("not-an-address") == []
