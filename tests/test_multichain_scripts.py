"""Multi-chain scripts: aggregation window, seed validity, and report links."""

from __future__ import annotations

import csv
import importlib.util
import inspect
import sys
from decimal import Decimal
from pathlib import Path

from wallet_analyzer.inputs import load_file
from wallet_analyzer.models import Network
from wallet_analyzer.tokens import DEFAULT_BEP20_TOKENS
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


def test_aggregate_drops_scanned_token_contracts(tmp_path):
    module = _load("batch_scan_tokens", "batch_scan_tokens.py")
    path = tmp_path / "DAI_matches.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(module.REPORT_COLUMNS)
        writer.writerow(
            [
                "0x6B175474E89094C44Da98b954EedeAC495271d0F",
                "BNB Smart Chain",
                "0",
                "50856.51",
                "50856.51",
                "PARTIAL",
            ]
        )
        writer.writerow(["0xaaa", "Ethereum", "100000", "0", "100000.00", "VERIFIED"])

    skipped: set[str] = set()
    entries = module.aggregate(
        [("DAI", path)], Decimal("50000"), Decimal("900000"), skipped=skipped
    )

    assert [entry["wallet"] for entry in entries] == ["0xaaa"]
    assert skipped == {"0x6b175474e89094c44da98b954eedeac495271d0f"}


def test_aggregate_excludes_extra_token_addresses(tmp_path):
    module = _load("batch_scan_tokens", "batch_scan_tokens.py")
    token = "0x55d398326f99059fF775485246999027B3197955"
    path = tmp_path / "TOKEN_matches.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(module.REPORT_COLUMNS)
        writer.writerow([token, "BNB Smart Chain", "0", "200000", "200000.00", "VERIFIED"])
        writer.writerow(["0xbbb", "BNB Smart Chain", "0", "200000", "200000.00", "VERIFIED"])

    entries = module.aggregate(
        [("TOKEN", path)],
        Decimal("50000"),
        Decimal("900000"),
        excluded={token.lower()},
    )

    assert [entry["wallet"] for entry in entries] == ["0xbbb"]


def test_scanned_contracts_cover_the_pipeline_tokens():
    scanner = _load("batch_scan_tokens", "batch_scan_tokens.py")
    pipeline = _load("run_pipeline", "run_pipeline.py")

    addresses = {address.lower() for _, address, _ in pipeline.TOKENS}
    assert addresses <= set(scanner.SCANNED_TOKENS)


def test_load_exchange_labels_reads_the_registry(tmp_path):
    module = _load("batch_scan_tokens", "batch_scan_tokens.py")
    path = tmp_path / "exchange_wallets.csv"
    path.write_text(
        "address,label,source\n"
        "0x18E296053CbDF986196903e889b7DcA7A73882F6,ByBit Hot Wallet 5,trace\n"
        "not-an-address,Ignored,-\n"
        "0xf89d7b9c864f589bbF53a82105107622B35EaA40,,\n",
        encoding="utf-8",
    )

    labels = module.load_exchange_labels(path)

    assert labels == {
        "0x18e296053cbdf986196903e889b7dca7a73882f6": "ByBit Hot Wallet 5",
        "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "exchange",
    }


def test_load_exchange_labels_tolerates_a_missing_registry(tmp_path):
    module = _load("batch_scan_tokens", "batch_scan_tokens.py")

    assert module.load_exchange_labels(tmp_path / "absent.csv") == {}


def test_drop_exchange_wallets_removes_labelled_addresses():
    module = _load("batch_scan_tokens", "batch_scan_tokens.py")
    entries = [
        {"wallet": "0x18E296053CbDF986196903e889b7DcA7A73882F6", "total": Decimal("74989")},
        {"wallet": "0xAAA0000000000000000000000000000000000001", "total": Decimal("50000")},
    ]

    kept, dropped = module.drop_exchange_wallets(
        entries, {"0x18e296053cbdf986196903e889b7dca7a73882f6": "ByBit Hot Wallet 5"}
    )

    assert [entry["wallet"] for entry in kept] == ["0xAAA0000000000000000000000000000000000001"]
    assert dropped == [("0x18E296053CbDF986196903e889b7DcA7A73882F6", "ByBit Hot Wallet 5")]


def test_exchange_registry_contains_the_traced_bybit_wallets():
    module = _load("batch_scan_tokens", "batch_scan_tokens.py")

    labels = module.load_exchange_labels()
    assert "0x18e296053cbdf986196903e889b7dca7a73882f6" in labels
    assert "0xf89d7b9c864f589bbf53a82105107622b35eaa40" in labels


def test_scanned_contracts_cover_the_bep20_allowlist():
    scanner = _load("batch_scan_tokens", "batch_scan_tokens.py")

    assert {token.key for token in DEFAULT_BEP20_TOKENS} <= set(scanner.SCANNED_TOKENS)
    assert set(scanner.SCANNED_TOKENS) == {token.key for token in DEFAULT_BEP20_TOKENS} | {
        "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "0x6b175474e89094c44da98b954eedeac495271d0f",
    }


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


def test_pipeline_defaults_match_the_standard_window():
    pipeline = _load("run_pipeline", "run_pipeline.py")
    args = pipeline.build_parser().parse_args([])
    assert args.min_usd == Decimal("50000")
    assert args.max_usd == Decimal("900000")

    report = _load("generate_report", "generate_report.py")
    default_max = inspect.signature(report.render).parameters["maximum"].default
    assert default_max == Decimal("900000")


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
