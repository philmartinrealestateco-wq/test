"""Runtime configuration, loaded from ``.env`` plus process environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOLANA_RPC = "https://api.mainnet-beta.solana.com"
DEFAULT_BSC_RPC = "https://bsc-dataseed.binance.org"


class ConfigError(RuntimeError):
    """Raised when configuration is present but unusable."""


def _decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default).strip() or default
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc


def _integer(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip() or str(default)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip() or str(default)
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def _secret(name: str, minimum: int = 10) -> str | None:
    """Read a credential, validating its shape without ever echoing it."""

    value = (os.getenv(name) or "").strip()
    if not value:
        return None
    if len(value) < minimum:
        raise ConfigError(f"{name} looks truncated ({len(value)} characters)")
    return value


def _presence(value: str | None) -> str:
    return "configured" if value else "absent"


@dataclass(frozen=True)
class Settings:
    etherscan_api_key: str | None
    bscscan_api_key: str | None
    blockchain_api_key: str | None
    solana_rpc_url: str
    bsc_rpc_url: str
    min_usd: Decimal
    max_usd: Decimal
    activity_days: int
    max_workers: int
    request_timeout: float
    price_ttl_seconds: int
    allow_keyless: bool
    output_dir: Path
    cache_dir: Path
    log_level: str
    bep20_enabled: bool = True
    token_list_file: Path | None = None

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

        min_usd = _decimal("MIN_USD", "50000")
        max_usd = _decimal("MAX_USD", "900000")
        if min_usd >= max_usd:
            raise ConfigError(
                f"MIN_USD ({min_usd}) must be lower than MAX_USD ({max_usd})"
            )

        activity_days = _integer("ACTIVITY_DAYS", 30)
        if activity_days <= 0:
            raise ConfigError("ACTIVITY_DAYS must be positive")

        max_workers = _integer("MAX_WORKERS", 4)
        if not 1 <= max_workers <= 32:
            raise ConfigError("MAX_WORKERS must be between 1 and 32")

        key = (os.getenv("ETHERSCAN_API_KEY") or "").strip()
        if key and len(key) < 10:
            raise ConfigError("ETHERSCAN_API_KEY looks truncated")

        return cls(
            etherscan_api_key=key or None,
            bscscan_api_key=_secret("BSCSCAN_API_KEY"),
            blockchain_api_key=_secret("BLOCKCHAIN_EXPLORER_API_KEY"),
            solana_rpc_url=(os.getenv("SOLANA_RPC_URL") or DEFAULT_SOLANA_RPC).strip(),
            bsc_rpc_url=(os.getenv("BSC_RPC_URL") or DEFAULT_BSC_RPC).strip(),
            min_usd=min_usd,
            max_usd=max_usd,
            activity_days=activity_days,
            max_workers=max_workers,
            request_timeout=_float("REQUEST_TIMEOUT", 20.0),
            price_ttl_seconds=_integer("PRICE_CACHE_TTL", 300),
            allow_keyless=_boolean("ALLOW_KEYLESS", True),
            output_dir=Path(os.getenv("OUTPUT_DIR") or PROJECT_ROOT / "out"),
            cache_dir=Path(os.getenv("CACHE_DIR") or PROJECT_ROOT / "cache"),
            log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(),
            bep20_enabled=_boolean("BEP20_ENABLED", True),
            token_list_file=(
                Path(token_file) if (token_file := (os.getenv("TOKEN_LIST_FILE") or "").strip()) else None
            ),
        )

    @property
    def has_etherscan_key(self) -> bool:
        return bool(self.etherscan_api_key)

    @property
    def bsc_activity_key(self) -> str | None:
        """Key used for BNB Chain history.

        Etherscan V2 is one unified API, so a dedicated BSCSCAN_API_KEY wins when
        present, otherwise the Etherscan key is tried against ``chainid=56``.
        """

        return self.bscscan_api_key or self.etherscan_api_key

    @property
    def has_bsc_activity_key(self) -> bool:
        return bool(self.bsc_activity_key)

    def with_overrides(
        self,
        *,
        min_usd: Decimal | None = None,
        max_usd: Decimal | None = None,
        activity_days: int | None = None,
        max_workers: int | None = None,
        output_dir: Path | None = None,
    ) -> "Settings":
        from dataclasses import replace

        changes: dict[str, object] = {}
        if min_usd is not None:
            changes["min_usd"] = min_usd
        if max_usd is not None:
            changes["max_usd"] = max_usd
        if activity_days is not None:
            changes["activity_days"] = activity_days
        if max_workers is not None:
            changes["max_workers"] = max_workers
        if output_dir is not None:
            changes["output_dir"] = output_dir

        updated = replace(self, **changes)
        if updated.min_usd >= updated.max_usd:
            raise ConfigError(
                f"minimum balance ({updated.min_usd}) must be lower than maximum ({updated.max_usd})"
            )
        if updated.activity_days <= 0:
            raise ConfigError("activity window must be a positive number of days")
        return updated

    def public_summary(self) -> dict[str, str]:
        """Config safe to print or embed in a report: never includes key material."""
        return {
            "etherscan_key": _presence(self.etherscan_api_key),
            "bscscan_key": _presence(self.bscscan_api_key),
            "blockchain_key": _presence(self.blockchain_api_key),
            "bsc_activity": (
                "BSCSCAN_API_KEY"
                if self.bscscan_api_key
                else "inherited from ETHERSCAN_API_KEY"
                if self.etherscan_api_key
                else "absent"
            ),
            "solana_rpc": _host_only(self.solana_rpc_url),
            "bsc_rpc": _host_only(self.bsc_rpc_url),
            "min_usd": f"{self.min_usd}",
            "max_usd": f"{self.max_usd}",
            "activity_days": str(self.activity_days),
            "max_workers": str(self.max_workers),
            "log_level": self.log_level,
            "bep20": "enabled" if self.bep20_enabled else "disabled",
        }


def _host_only(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.hostname or "invalid-url"
