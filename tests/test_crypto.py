"""Primitive tests: Keccak-256, base58check and bech32 decoding."""

from __future__ import annotations

import pytest

from wallet_analyzer.crypto import (
    base58_decode,
    bech32_decode_segwit,
    keccak256,
    to_checksum_address,
)


def test_keccak256_empty_string_vector() -> None:
    assert keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )


def test_keccak256_abc_vector() -> None:
    assert keccak256(b"abc").hex() == (
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    )


@pytest.mark.parametrize(
    "address",
    [
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
        "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
    ],
)
def test_eip55_vectors(address: str) -> None:
    assert to_checksum_address(address) == address


def test_base58_decodes_leading_zeros() -> None:
    decoded = base58_decode("1FeexV6bAHb8ybZjqQMjJrcCrHGW9sb6uF")
    assert len(decoded) == 25
    assert decoded[0] == 0x00


def test_base58_rejects_invalid_character() -> None:
    with pytest.raises(ValueError):
        base58_decode("0OIl")


@pytest.mark.parametrize(
    "address",
    [
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4",
        "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3",
        "bc1pqv53yj63f2vj9ft9lsn0rx4h8rhv8pq7qkj8fqdv5ud0jl0ecjfqy6rxre",
    ],
)
def test_bech32_accepts_valid_segwit(address: str) -> None:
    decoded = bech32_decode_segwit(address)
    assert decoded is not None
    version, program = decoded
    assert 0 <= version <= 1
    assert len(program) in (20, 32)


def test_bech32_rejects_broken_checksum() -> None:
    assert bech32_decode_segwit("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5") is None


def test_bech32_rejects_testnet_prefix() -> None:
    assert bech32_decode_segwit("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx") is None
