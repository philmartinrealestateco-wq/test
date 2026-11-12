"""Address input: CSV/TSV files, plain text lists, and CLI values.

A row may name its network. When it does not, the format is used to infer the
chain; a 0x address is expanded to both Ethereum and BNB Smart Chain because
those share one address space and are genuinely different wallets.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .models import AddressEntry, Network
from .validators import detect_networks

_ADDRESS_KEYS = ("address", "wallet", "wallet_address", "addr", "account")
_NETWORK_KEYS = ("network", "chain", "blockchain", "net")
_LABEL_KEYS = ("label", "name", "note", "description")


class InputError(RuntimeError):
    """The address file could not be read or has no usable address column."""


@dataclass(frozen=True)
class RawAddress:
    address: str
    network: Network | None = None
    label: str = ""


@dataclass
class ParsedInput:
    entries: list[AddressEntry]
    warnings: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        return len(self.warnings)


def resolve(raw_addresses: list[RawAddress]) -> ParsedInput:
    entries: list[AddressEntry] = []
    warnings: list[str] = []

    for raw in raw_addresses:
        address = raw.address.strip()
        if not address or address.startswith("#"):
            continue

        if raw.network is not None:
            entries.append(AddressEntry(network=raw.network, address=address, label=raw.label))
            continue

        candidates = detect_networks(address)
        if not candidates:
            warnings.append(f"unrecognised address format, skipped: {address}")
            continue
        for network in candidates:
            entries.append(AddressEntry(network=network, address=address, label=raw.label))

    return ParsedInput(entries=entries, warnings=warnings)


def load_file(path: Path) -> ParsedInput:
    if not path.exists():
        raise InputError(f"input file not found: {path}")
    if path.is_dir():
        raise InputError(f"input path is a directory: {path}")

    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        raw, warnings = _read_table(path, delimiter="\t" if suffix == ".tsv" else ",")
    else:
        raw, warnings = _read_lines(path)

    parsed = resolve(raw)
    parsed.warnings = warnings + parsed.warnings
    return parsed


def _read_table(path: Path, *, delimiter: str) -> tuple[list[RawAddress], list[str]]:
    warnings: list[str] = []
    rows: list[RawAddress] = []

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames is None:
            raise InputError(f"{path.name} appears to be empty")

        lookup = {name.strip().lower(): name for name in reader.fieldnames if name}
        address_column = _first_present(lookup, _ADDRESS_KEYS)
        if address_column is None:
            raise InputError(
                f"{path.name} needs an 'address' column; found: "
                + ", ".join(lookup.values())
            )
        network_column = _first_present(lookup, _NETWORK_KEYS)
        label_column = _first_present(lookup, _LABEL_KEYS)

        for line_number, row in enumerate(reader, start=2):
            address = (row.get(lookup[address_column]) or "").strip()
            if not address or address.startswith("#"):
                continue

            network: Network | None = None
            if network_column:
                raw_network = (row.get(lookup[network_column]) or "").strip()
                if raw_network:
                    try:
                        network = Network.parse(raw_network)
                    except ValueError as exc:
                        warnings.append(f"{path.name} line {line_number}: {exc}")
                        continue

            label = (row.get(lookup[label_column]) or "").strip() if label_column else ""
            rows.append(RawAddress(address=address, network=network, label=label))

    return rows, warnings


def _read_lines(path: Path) -> tuple[list[RawAddress], list[str]]:
    warnings: list[str] = []
    rows: list[RawAddress] = []

    for line_number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        text = line.strip()
        if not text or text.startswith("#"):
            continue

        network: Network | None = None
        address = text
        if ":" in text:
            prefix, _, remainder = text.partition(":")
            try:
                network = Network.parse(prefix)
                address = remainder.strip()
            except ValueError:
                address = text

        if not address:
            warnings.append(f"{path.name} line {line_number}: no address on this line")
            continue
        rows.append(RawAddress(address=address, network=network))

    return rows, warnings


def from_argument(value: str, network: Network | None = None) -> RawAddress:
    address = value.strip()
    resolved_network = network
    if resolved_network is None and ":" in address:
        prefix, _, remainder = address.partition(":")
        try:
            resolved_network = Network.parse(prefix)
            address = remainder.strip()
        except ValueError:
            pass
    return RawAddress(address=address, network=resolved_network)


def _first_present(lookup: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in lookup:
            return lookup[key]
    return None
