"""Keccak-256, base58 and bech32 primitives.

Implemented locally so the tool has no crypto/Web3 dependency just to validate an
address. The Keccak permutation below is the original padding variant used by
Ethereum (NIST SHA3-256 is *not* interchangeable), and it is covered by the
published test vectors in ``tests/test_crypto.py``.
"""

from __future__ import annotations

_MASK64 = (1 << 64) - 1

_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

_ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: index for index, char in enumerate(B58_ALPHABET)}
# Ripple uses a different base58 alphabet; its 'r' is index 0, not Bitcoin's '1'.
XRP_B58_ALPHABET = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
_XRP_B58_INDEX = {char: index for index, char in enumerate(XRP_B58_ALPHABET)}

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_INDEX = {char: index for index, char in enumerate(_BECH32_CHARSET)}
_BECH32_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


def _rol(value: int, shift: int) -> int:
    shift %= 64
    return ((value << shift) | (value >> (64 - shift))) & _MASK64 if shift else value


def _keccak_f(state: list[list[int]]) -> None:
    for round_constant in _ROUND_CONSTANTS:
        columns = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        deltas = [columns[(x - 1) % 5] ^ _rol(columns[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= deltas[x]

        rotated = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                rotated[y][(2 * x + 3 * y) % 5] = _rol(state[x][y], _ROTATIONS[x][y])

        for x in range(5):
            for y in range(5):
                state[x][y] = rotated[x][y] ^ ((~rotated[(x + 1) % 5][y]) & rotated[(x + 2) % 5][y])

        state[0][0] ^= round_constant


def keccak256(data: bytes) -> bytes:
    """Keccak-256 as used for Ethereum address checksums and transaction hashes."""

    rate = 136
    state = [[0] * 5 for _ in range(5)]

    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate:
        padded.append(0x00)
    padded[-1] |= 0x80

    for offset in range(0, len(padded), rate):
        block = padded[offset : offset + rate]
        for lane in range(rate // 8):
            x, y = lane % 5, lane // 5
            state[x][y] ^= int.from_bytes(block[lane * 8 : lane * 8 + 8], "little")
        _keccak_f(state)

    return b"".join(state[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def to_checksum_address(address: str) -> str:
    """EIP-55 mixed-case form of a 20-byte hex address."""

    body = address.lower().removeprefix("0x")
    if len(body) != 40:
        raise ValueError(f"not a 20-byte hex address: {address!r}")
    digest = keccak256(body.encode("ascii")).hex()
    checksummed = "".join(
        char.upper() if int(digest[index], 16) >= 8 else char
        for index, char in enumerate(body)
    )
    return "0x" + checksummed


def base58_decode(value: str) -> bytes:
    number = 0
    for char in value:
        try:
            number = number * 58 + _B58_INDEX[char]
        except KeyError as exc:
            raise ValueError(f"invalid base58 character {char!r}") from exc
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeros + body


def base58_decode_xrp(value: str) -> bytes:
    """Decode a classic XRP address using the Ripple base58 alphabet."""

    number = 0
    for char in value:
        try:
            number = number * 58 + _XRP_B58_INDEX[char]
        except KeyError as exc:
            raise ValueError(f"invalid XRP base58 character {char!r}") from exc
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(value) - len(value.lstrip("r"))
    return b"\x00" * leading_zeros + body


def bech32_decode_segwit(address: str) -> tuple[int, bytes] | None:
    """Return ``(witness_version, program)`` for a valid mainnet bech32 address."""

    if not 14 <= len(address) <= 90:
        return None
    if address.lower() != address and address.upper() != address:
        return None
    if any(ord(char) < 33 or ord(char) > 126 for char in address):
        return None

    lowered = address.lower()
    separator = lowered.rfind("1")
    if separator < 1 or separator + 7 > len(lowered):
        return None

    hrp = lowered[:separator]
    if hrp != "bc":
        return None

    try:
        data = [_BECH32_INDEX[char] for char in lowered[separator + 1 :]]
    except KeyError:
        return None

    checksum = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if checksum not in (_BECH32_CONST, _BECH32M_CONST):
        return None

    witness_version = data[0]
    if witness_version > 16:
        return None
    program = _convert_bits(data[1:-6], 5, 8, pad=False)
    if program is None or not 2 <= len(program) <= 40:
        return None
    if witness_version == 0:
        if len(program) not in (20, 32) or checksum != _BECH32_CONST:
            return None
    elif checksum != _BECH32M_CONST:
        return None

    return witness_version, bytes(program)


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _bech32_polymod(values: list[int]) -> int:
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index in range(5):
            if (top >> index) & 1:
                checksum ^= _BECH32_GENERATOR[index]
    return checksum


def _convert_bits(data: list[int], from_bits: int, to_bits: int, pad: bool) -> list[int] | None:
    accumulator = 0
    bit_count = 0
    output: list[int] = []
    max_value = (1 << to_bits) - 1

    for value in data:
        if value < 0 or value >> from_bits:
            return None
        accumulator = (accumulator << from_bits) | value
        bit_count += from_bits
        while bit_count >= to_bits:
            bit_count -= to_bits
            output.append((accumulator >> bit_count) & max_value)

    if pad:
        if bit_count:
            output.append((accumulator << (to_bits - bit_count)) & max_value)
    elif bit_count >= from_bits or ((accumulator << (to_bits - bit_count)) & max_value):
        return None

    return output
