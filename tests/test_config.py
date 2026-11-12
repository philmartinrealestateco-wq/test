"""Credential resolution: presence, fallbacks, and never leaking values."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wallet_analyzer.config import ConfigError, Settings

SECRETS = {
    "ETHERSCAN_API_KEY": "ETHERSCAN-FAKE-KEY-0001",
    "BSCSCAN_API_KEY": "BSCSCAN-FAKE-KEY-0002",
    "BLOCKCHAIN_EXPLORER_API_KEY": "BLOCKCHAIN-FAKE-KEY-0003",
}


@pytest.fixture
def clean_env(monkeypatch, tmp_path: Path):
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ALLOW_KEYLESS", raising=False)
    return tmp_path / "absent.env"


def load(env_file: Path) -> Settings:
    return Settings.load(env_file)


def test_no_keys_is_a_valid_configuration(clean_env: Path) -> None:
    settings = load(clean_env)

    assert settings.etherscan_api_key is None
    assert settings.bscscan_api_key is None
    assert settings.blockchain_api_key is None
    assert settings.bsc_activity_key is None
    assert settings.has_bsc_activity_key is False


def test_etherscan_key_is_reused_for_bsc(clean_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("ETHERSCAN_API_KEY", SECRETS["ETHERSCAN_API_KEY"])
    settings = load(clean_env)

    assert settings.has_etherscan_key
    assert settings.bsc_activity_key == SECRETS["ETHERSCAN_API_KEY"]
    assert settings.public_summary()["bsc_activity"] == "inherited from ETHERSCAN_API_KEY"


def test_dedicated_bsc_key_wins(clean_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("ETHERSCAN_API_KEY", SECRETS["ETHERSCAN_API_KEY"])
    monkeypatch.setenv("BSCSCAN_API_KEY", SECRETS["BSCSCAN_API_KEY"])
    settings = load(clean_env)

    assert settings.bsc_activity_key == SECRETS["BSCSCAN_API_KEY"]
    assert settings.public_summary()["bsc_activity"] == "BSCSCAN_API_KEY"


def test_blockchain_key_is_optional(clean_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLOCKCHAIN_EXPLORER_API_KEY", SECRETS["BLOCKCHAIN_EXPLORER_API_KEY"])
    settings = load(clean_env)

    assert settings.blockchain_api_key == SECRETS["BLOCKCHAIN_EXPLORER_API_KEY"]
    assert settings.public_summary()["blockchain_key"] == "configured"


def test_truncated_key_is_rejected(clean_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("ETHERSCAN_API_KEY", "short")
    with pytest.raises(ConfigError, match="truncated"):
        load(clean_env)


def test_public_summary_never_contains_key_material(clean_env: Path, monkeypatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SOLANA_RPC_URL", "https://example.helius-rpc.com/?api-key=SECRET-URL-TOKEN")

    summary = load(clean_env).public_summary()
    serialized = json.dumps(summary)

    for value in SECRETS.values():
        assert value not in serialized
    assert "SECRET-URL-TOKEN" not in serialized
    assert summary["solana_rpc"] == "example.helius-rpc.com"


def test_endpoint_is_reported_by_host_only(clean_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=abc123")
    assert load(clean_env).public_summary()["solana_rpc"] == "mainnet.helius-rpc.com"
