"""Address validation and network detection.

Validation happens before any network call so a typo never costs a request, and
so a malformed address is reported as ``INVALID_ADDRESS`` rather than as a
lookup failure.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .crypto import base58_decode, base58_decode_xrp, bech32_decode_segwit, to_checksum_address
from .models import Network

_EVM_HEX = re.compile(r"^0x[0-9a-fA-F]{40}$")
_LEGACY_BTC = re.compile(r"^[13][1-9A-HJ-NP-Za-km-z]{25,34}$")
_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_XRP_ADDRESS = re.compile(r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$")


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    normalized: str
    reason: str = ""


def _bitcoin_checksum_ok(payload: bytes) -> bool:
    """base58check: the trailing 4 bytes are sha256(sha256(body))[:4]."""

    body, checksum = payload[:-4], payload[-4:]
    digest = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    return checksum == digest


def validate_bitcoin(address: str) -> ValidationResult:
    candidate = address.strip()

    if candidate.lower().startswith("bc1"):
        decoded = bech32_decode_segwit(candidate)
        if decoded is None:
            return ValidationResult(False, address, "bech32 checksum or witness program is invalid")
        return ValidationResult(True, candidate.lower())

    if not _LEGACY_BTC.match(candidate):
        return ValidationResult(False, address, "not a Bitcoin base58 or bech32 address")

    try:
        decoded = base58_decode(candidate)
    except ValueError as exc:
        return ValidationResult(False, address, str(exc))

    if len(decoded) != 25:
        return ValidationResult(False, address, "decoded payload is not 25 bytes")
    if decoded[0] not in (0x00, 0x05):
        return ValidationResult(False, address, "not a mainnet P2PKH/P2SH version byte")
    if not _bitcoin_checksum_ok(decoded):
        return ValidationResult(False, address, "base58check checksum mismatch")

    return ValidationResult(True, candidate)


def validate_evm(address: str) -> ValidationResult:
    candidate = address.strip()
    if not _EVM_HEX.match(candidate):
        return ValidationResult(False, address, "expected 0x followed by 40 hex characters")

    body = candidate[2:]
    if body.islower() or body.isupper():
        # Unchecksummed input is accepted; we echo back the EIP-55 form.
        return ValidationResult(True, to_checksum_address(candidate))

    try:
        expected = to_checksum_address(candidate)
    except ValueError as exc:  # pragma: no cover - guarded by the regex above
        return ValidationResult(False, address, str(exc))

    if expected != candidate:
        return ValidationResult(False, address, "EIP-55 checksum mismatch (possible typo)")
    return ValidationResult(True, candidate)


def validate_solana(address: str) -> ValidationResult:
    candidate = address.strip()
    if not _BASE58.match(candidate):
        return ValidationResult(False, address, "not a base58 string of 32-44 characters")
    try:
        decoded = base58_decode(candidate)
    except ValueError as exc:
        return ValidationResult(False, address, str(exc))
    if len(decoded) != 32:
        return ValidationResult(False, address, "Solana public keys decode to 32 bytes")
    return ValidationResult(True, candidate)


def validate_xrp(address: str) -> ValidationResult:
    """Classic ``r...`` XRP account addresses only; X-addresses encode a tag."""

    candidate = address.strip()
    if candidate.startswith("X"):
        return ValidationResult(
            False,
            address,
            "X-addresses are not supported; use the classic r... account address",
        )
    if not _XRP_ADDRESS.match(candidate):
        return ValidationResult(False, address, "not a classic XRP r... address")

    try:
        decoded = base58_decode_xrp(candidate)
    except ValueError as exc:
        return ValidationResult(False, address, str(exc))

    if len(decoded) != 25:
        return ValidationResult(False, address, "decoded payload is not 25 bytes")
    if decoded[0] != 0x00:
        return ValidationResult(False, address, "not a mainnet XRP account version byte")
    if not _bitcoin_checksum_ok(decoded):
        return ValidationResult(False, address, "base58check checksum mismatch")

    return ValidationResult(True, candidate)


def validate(network: Network, address: str) -> ValidationResult:
    if not address or not address.strip():
        return ValidationResult(False, address, "empty address")
    if network is Network.BITCOIN:
        return validate_bitcoin(address)
    if network in (Network.ETHEREUM, Network.BSC):
        return validate_evm(address)
    if network is Network.SOLANA:
        return validate_solana(address)
    if network is Network.XRP:
        return validate_xrp(address)
    return ValidationResult(False, address, f"unsupported network {network}")


def detect_networks(address: str) -> list[Network]:
    """Best-effort network detection when the input does not name a chain.

    A 0x address is genuinely ambiguous between Ethereum and BNB Smart Chain, so
    both candidates are returned and the caller analyses each separately.
    """

    candidate = address.strip()
    if _EVM_HEX.match(candidate):
        return [Network.ETHEREUM, Network.BSC]
    if candidate.lower().startswith("bc1"):
        return [Network.BITCOIN] if validate_bitcoin(candidate).valid else []
    if _LEGACY_BTC.match(candidate) and validate_bitcoin(candidate).valid:
        return [Network.BITCOIN]
    if validate_xrp(candidate).valid:
        return [Network.XRP]
    if validate_solana(candidate).valid:
        return [Network.SOLANA]
    return []
