"""Input parsing for CSV/TSV/TXT files and CLI values."""

from __future__ import annotations

from pathlib import Path

import pytest

from wallet_analyzer.inputs import InputError, from_argument, load_file, resolve
from wallet_analyzer.models import Network

VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
BECH32 = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"


def write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_csv_with_explicit_network(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "a.csv",
        "address,network,label\n"
        f"{VITALIK},ethereum,whale\n"
        f"{BECH32},bitcoin,btc-demo\n",
    )
    parsed = load_file(path)
    assert [entry.network for entry in parsed.entries] == [Network.ETHEREUM, Network.BITCOIN]
    assert parsed.entries[0].label == "whale"
    assert not parsed.warnings


def test_csv_without_network_expands_evm(tmp_path: Path) -> None:
    path = write(tmp_path, "b.csv", f"address\n{VITALIK}\n")
    parsed = load_file(path)
    assert [entry.network for entry in parsed.entries] == [Network.ETHEREUM, Network.BSC]


def test_holder_export_header_is_accepted(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "holders.csv",
        "HolderAddress,Balance,PendingBalanceUpdate\n"
        f"{VITALIK},12345,0\n",
    )
    parsed = load_file(path)

    assert [entry.network for entry in parsed.entries] == [
        Network.ETHEREUM,
        Network.BSC,
    ]
    assert parsed.entries[0].address == VITALIK


def test_csv_unknown_network_warns_and_skips(tmp_path: Path) -> None:
    path = write(tmp_path, "c.csv", f"address,chain\n{VITALIK},dogecoin\n")
    parsed = load_file(path)
    assert parsed.entries == []
    assert len(parsed.warnings) == 1
    assert "dogecoin" in parsed.warnings[0]


def test_csv_requires_address_column(tmp_path: Path) -> None:
    path = write(tmp_path, "d.csv", "wallet_address,chain\nabc,eth\n")
    parsed = load_file(path)
    assert parsed.entries[0].address == "abc"


def test_csv_with_no_usable_column_raises(tmp_path: Path) -> None:
    path = write(tmp_path, "e.csv", "foo,bar\n1,2\n")
    with pytest.raises(InputError):
        load_file(path)


def test_txt_supports_network_prefix(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "list.txt",
        f"# comment\nethereum:{VITALIK}\n\nbitcoin:{BECH32}\n",
    )
    parsed = load_file(path)
    assert [(e.network, e.address) for e in parsed.entries] == [
        (Network.ETHEREUM, VITALIK),
        (Network.BITCOIN, BECH32),
    ]


def test_txt_without_prefix_is_detected(tmp_path: Path) -> None:
    path = write(tmp_path, "plain.txt", f"{BECH32}\n")
    parsed = load_file(path)
    assert parsed.entries[0].network is Network.BITCOIN


def test_unrecognised_address_is_reported_as_warning(tmp_path: Path) -> None:
    path = write(tmp_path, "f.txt", "hello-world\n")
    parsed = load_file(path)
    assert parsed.entries == []
    assert "unrecognised" in parsed.warnings[0]


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        load_file(tmp_path / "nope.csv")


def test_from_argument_parses_prefix() -> None:
    raw = from_argument(f"solana:{BECH32}")
    assert raw.network is Network.SOLANA
    assert raw.address == BECH32


def test_resolve_ignores_blank_rows() -> None:
    parsed = resolve([from_argument(""), from_argument("   ")])
    assert parsed.entries == []
