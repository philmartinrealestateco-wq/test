"""Multi-chain scripts: aggregation window, seed validity, and report links."""

from __future__ import annotations

import csv
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

from wallet_analyzer.inputs import load_file
from wallet_analyzer.models import Network
from wallet_analyzer.validators import validate

ROOT = Path(__file__).resolve().parent.parent


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_aggregate_respects_a_custom_window(tmp_path):
    module = _load("batch_scan_tokens", "batch_scan_tokens.py")
    path = tmp_path / "TOKEN_matches.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(module.REPORT_COLUMNS)
        writer.writerow(["0xaaa", "Ethereum", "100000", "0", "500000.00", "VERIFIED"])
        writer.writerow(["0xbbb", "Ethereum", "7000000", "0", "7000000.00", "VERIFIED"])
        writer.writerow(["0xccc", "Ethereum", "12000000", "0", "12000000.00", "VERIFIED"])

    wide = module.aggregate([("TOKEN", path)], Decimal("50000"), Decimal("10000000"))
    narrow = module.aggregate([("TOKEN", path)], Decimal("50000"), Decimal("900000"))

    assert [entry["wallet"] for entry in wide] == ["0xbbb", "0xaaa"]
    assert [entry["wallet"] for entry in narrow] == ["0xaaa"]


def test_seed_addresses_validate_for_their_network():
    module = _load("fetch_btc_sol_holders", "fetch_btc_sol_holders.py")

    assert module.SEED
    for seed in module.SEED:
        outcome = validate(seed.network, seed.address)
        assert outcome.valid, f"{seed.address}: {outcome.reason}"


def test_guess_network_separates_bitcoin_from_solana():
    module = _load("fetch_btc_sol_holders", "fetch_btc_sol_holders.py")

    assert module._guess_network("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "") is Network.BITCOIN
    assert (
        module._guess_network("5FGoPPj1nL8LCnfVnpTmreqQtqLuMXXAwuS1uahMrp8V", "")
        is Network.SOLANA
    )
    assert module._guess_network("not-an-address", "") is None


def test_holders_csv_maps_each_row_to_its_network(tmp_path):
    path = tmp_path / "btc_sol_holders.csv"
    path.write_text(
        "address,network,balance,label\n"
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa,bitcoin,107.72,genesis\n"
        "5FGoPPj1nL8LCnfVnpTmreqQtqLuMXXAwuS1uahMrp8V,solana,0.0,sample\n",
        encoding="utf-8",
    )

    parsed = load_file(path)

    assert [entry.network for entry in parsed.entries] == [Network.BITCOIN, Network.SOLANA]
    assert parsed.warnings == []


def test_report_links_bitcoin_and_solana_plus_the_window():
    module = _load("generate_report", "generate_report.py")
    entries = [
        {
            "wallet": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
            "tokens": ["BTC"],
            "networks": ["Bitcoin"],
            "total": Decimal("7352273.50"),
            "status": "VERIFIED",
        },
        {
            "wallet": "5FGoPPj1nL8LCnfVnpTmreqQtqLuMXXAwuS1uahMrp8V",
            "tokens": ["SOL"],
            "networks": ["Solana"],
            "total": Decimal("120000.00"),
            "status": "PARTIAL",
        },
    ]

    page = module.render(
        entries, Path("out/master_targets.csv"), Decimal("50000"), Decimal("10000000")
    )

    assert "blockchain.com/explorer/addresses/btc/1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa" in page
    assert "solscan.io/account/5FGoPPj1nL8LCnfVnpTmreqQtqLuMXXAwuS1uahMrp8V" in page
    assert "$50,000–$10,000,000" in page
    assert "Bitcoin legs" in page and "Solana legs" in page
