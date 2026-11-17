"""Core value objects shared by every provider, the analyzer and the reporters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Network(str, Enum):
    BITCOIN = "bitcoin"
    ETHEREUM = "ethereum"
    BSC = "bsc"
    SOLANA = "solana"
    XRP = "xrp"

    @property
    def label(self) -> str:
        return {
            Network.BITCOIN: "Bitcoin",
            Network.ETHEREUM: "Ethereum",
            Network.BSC: "BNB Smart Chain",
            Network.SOLANA: "Solana",
            Network.XRP: "XRP",
        }[self]

    @property
    def native_symbol(self) -> str:
        return {
            Network.BITCOIN: "BTC",
            Network.ETHEREUM: "ETH",
            Network.BSC: "BNB",
            Network.SOLANA: "SOL",
            Network.XRP: "XRP",
        }[self]

    @property
    def decimals(self) -> int:
        return {
            Network.BITCOIN: 8,
            Network.ETHEREUM: 18,
            Network.BSC: 18,
            Network.SOLANA: 9,
            Network.XRP: 6,
        }[self]

    @property
    def is_evm(self) -> bool:
        return self in (Network.ETHEREUM, Network.BSC)

    def explorer_url(self, address: str) -> str:
        if self is Network.BITCOIN:
            return f"https://www.blockchain.com/explorer/addresses/btc/{address}"
        if self is Network.ETHEREUM:
            return f"https://etherscan.io/address/{address}"
        if self is Network.BSC:
            return f"https://bscscan.com/address/{address}"
        if self is Network.XRP:
            return f"https://xrpscan.com/account/{address}"
        return f"https://solscan.io/account/{address}"

    @classmethod
    def parse(cls, value: str) -> "Network":
        token = value.strip().lower()
        aliases = {
            "bitcoin": cls.BITCOIN,
            "btc": cls.BITCOIN,
            "ethereum": cls.ETHEREUM,
            "eth": cls.ETHEREUM,
            "bsc": cls.BSC,
            "bnb": cls.BSC,
            "binance": cls.BSC,
            "solana": cls.SOLANA,
            "sol": cls.SOLANA,
            "xrp": cls.XRP,
            "ripple": cls.XRP,
            "xrpl": cls.XRP,
        }
        if token not in aliases:
            raise ValueError(
                f"unknown network {value!r}; expected one of "
                f"{', '.join(sorted(aliases))}"
            )
        return aliases[token]


class VerificationStatus(str, Enum):
    """How much of a wallet's data was confirmed against a blockchain source.

    VERIFIED  balance and activity both came back from a chain-backed API
    PARTIAL   balance confirmed, activity could not be (e.g. BSC without a key)
    UNVERIFIED nothing could be confirmed, or the lookup failed
    INVALID   the address is not syntactically valid for the network
    """

    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    UNVERIFIED = "UNVERIFIED"
    INVALID = "INVALID_ADDRESS"


class ActivityStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AddressEntry:
    """One wallet the user asked us to look at."""

    network: Network
    address: str
    label: str = ""

    @property
    def dedupe_key(self) -> tuple[Network, str]:
        # EVM addresses are case-insensitive; base58 (BTC/SOL) is not.
        return (self.network, self.address.lower() if self.network.is_evm else self.address)


@dataclass
class TokenHolding:
    """One BEP-20 balance, valued separately from the native coin."""

    contract: str
    symbol: str
    decimals: int
    raw_balance: int
    amount: Decimal
    price_usd: Decimal | None = None
    usd_value: Decimal | None = None
    note: str = ""


@dataclass
class WalletRecord:
    """Everything we learned about a single wallet, including what we could not learn."""

    address: str
    network: Network
    balance: Decimal | None = None
    price_usd: Decimal | None = None
    usd_value: Decimal | None = None
    last_tx_at: datetime | None = None
    days_since_last_tx: int | None = None
    activity_status: ActivityStatus = ActivityStatus.UNKNOWN
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    data_source: str = ""
    notes: list[str] = field(default_factory=list)
    label: str = ""
    token_holdings: list[TokenHolding] = field(default_factory=list)
    tokens_usd: Decimal | None = None
    tokens_checked: bool = False
    token_scan_complete: bool = True
    token_note: str = ""
    balance_verified: bool = False
    activity_verified: bool = False

    @property
    def native_symbol(self) -> str:
        return self.network.native_symbol

    @property
    def is_active(self) -> bool:
        return self.activity_status is ActivityStatus.ACTIVE

    @property
    def token_count(self) -> int:
        return len(self.token_holdings)

    def classify(self) -> VerificationStatus:
        """Verification is only as strong as its weakest component."""

        if self.verification_status is VerificationStatus.INVALID:
            return VerificationStatus.INVALID
        tokens_complete = (not self.tokens_checked) or self.tokens_usd is not None
        if self.balance_verified and self.activity_verified and tokens_complete:
            return VerificationStatus.VERIFIED
        if self.balance_verified:
            return VerificationStatus.PARTIAL
        return VerificationStatus.UNVERIFIED

    @property
    def total_usd(self) -> Decimal | None:
        """Native plus priced tokens. ``None`` when the valuation is incomplete."""

        if not self.tokens_checked:
            return self.usd_value
        if self.usd_value is None or self.tokens_usd is None:
            return None
        return self.usd_value + self.tokens_usd

    def in_balance_range(self, minimum: Decimal, maximum: Decimal) -> bool:
        total = self.total_usd
        return total is not None and minimum <= total <= maximum

    def joined_notes(self) -> str:
        return "; ".join(note for note in self.notes if note)


@dataclass(frozen=True)
class AnalysisSettings:
    min_usd: Decimal
    max_usd: Decimal
    activity_days: int

    def __post_init__(self) -> None:
        if self.min_usd < 0:
            raise ValueError("min_usd must be >= 0")
        if self.max_usd <= self.min_usd:
            raise ValueError("max_usd must be greater than min_usd")
        if self.activity_days <= 0:
            raise ValueError("activity_days must be positive")

    def describe(self) -> str:
        return (
            f"balance {self.min_usd:,.0f}-{self.max_usd:,.0f} USD, "
            f"activity within {self.activity_days} days"
        )


@dataclass
class AnalysisResult:
    records: list[WalletRecord]
    matches: list[WalletRecord]
    settings: AnalysisSettings
    prices: dict[str, Decimal]
    price_source: str
    price_fetched_at: datetime | None
    started_at: datetime
    finished_at: datetime

    @property
    def total_addresses(self) -> int:
        return len(self.records)

    @property
    def verified_count(self) -> int:
        return sum(
            1 for r in self.records if r.verification_status is VerificationStatus.VERIFIED
        )

    @property
    def active_count(self) -> int:
        return sum(1 for r in self.records if r.activity_status is ActivityStatus.ACTIVE)

    @property
    def in_range_count(self) -> int:
        return sum(
            1
            for r in self.records
            if r.in_balance_range(self.settings.min_usd, self.settings.max_usd)
        )

    @property
    def matched_total_usd(self) -> Decimal:
        return sum((r.total_usd or Decimal(0) for r in self.matches), Decimal(0))

    @property
    def token_value_usd(self) -> Decimal:
        """Sum of priced BEP-20 holdings across analysed wallets."""

        return sum((r.tokens_usd or Decimal(0) for r in self.records), Decimal(0))

    @property
    def wallets_with_tokens(self) -> int:
        return sum(1 for r in self.records if r.token_holdings)

    def network_distribution(self) -> dict[Network, int]:
        counts = {network: 0 for network in Network}
        for record in self.records:
            counts[record.network] += 1
        return {network: count for network, count in counts.items() if count}


_HEX_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def looks_like_evm_address(address: str) -> bool:
    return bool(_HEX_ADDRESS.match(address.strip()))
