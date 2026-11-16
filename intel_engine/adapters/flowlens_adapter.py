"""FlowLens adapter: turn a case export into per-transfer evidence.

FlowLens keeps each transfer as a record with the transaction hash, block, UTC
timestamp, token contract and the provider that supplied it. This adapter only
parses - storage and reporting live in ``flowlens.py`` and ``report_generator``.

A case without transfers is only acceptable when its coverage says the provider
answered; otherwise the caller gets an error instead of an empty trace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

EXPLORERS = {
    1: "https://etherscan.io",
    56: "https://bscscan.com",
    8453: "https://basescan.org",
    42161: "https://arbiscan.io",
}


class FlowLensError(RuntimeError):
    """The case file could not be read or carries no usable evidence."""


@dataclass(frozen=True)
class Transfer:
    chain_id: int
    tx_hash: str
    event_index: str
    block_number: int | None
    block_time: str | None
    direction: str
    counterparty: str
    amount: str
    asset: str
    kind: str
    provider: str
    source_url: str | None


@dataclass(frozen=True)
class Coverage:
    provider: str
    status: str
    pages: int
    chain_id: int
    address: str


@dataclass
class FlowLensCase:
    address: str
    mode: str | None
    updated_at: str | None
    transfers: list[Transfer] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    skipped: int = 0

    @property
    def partial(self) -> bool:
        return any(entry.status != "complete" for entry in self.coverage)

    @property
    def pages(self) -> int:
        return sum(entry.pages for entry in self.coverage)

    def coverage_note(self) -> str:
        if not self.coverage:
            return "no provider coverage recorded"
        parts = [
            f"{entry.provider} {entry.status}, {entry.pages} page(s)"
            for entry in self.coverage
        ]
        return "; ".join(dict.fromkeys(parts))


def format_amount(raw: object, decimals: object) -> str:
    """Render a raw integer amount with its token decimals, without noise."""
    try:
        value = Decimal(str(raw)) / (Decimal(10) ** int(decimals or 0))
    except (InvalidOperation, ValueError, TypeError):
        return str(raw)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def explorer_url(chain_id: int, tx_hash: str) -> str | None:
    base = EXPLORERS.get(chain_id)
    return f"{base}/tx/{tx_hash}" if base and tx_hash else None


def load_case(path: str | Path, address: str | None = None) -> FlowLensCase:
    """Parse one FlowLens case export, keeping transfers that touch ``address``."""
    case_path = Path(path)
    try:
        payload = json.loads(case_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise FlowLensError(f"could not read {case_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise FlowLensError(f"{case_path.name} is not valid JSON: {error}") from error

    root = str(payload.get("root") or "").strip().lower()
    focus = str(address or root).strip().lower()
    if not focus:
        raise FlowLensError("the case has no root address and none was supplied")

    transfers: list[Transfer] = []
    skipped = 0
    for item in payload.get("transfers") or []:
        sender = str(item.get("from") or "").lower()
        recipient = str(item.get("to") or "").lower()
        if focus not in (sender, recipient):
            skipped += 1
            continue

        asset = item.get("asset")
        if not isinstance(asset, dict):
            asset = {"symbol": str(asset)} if asset else {}
        chain_id = int(item.get("chainId") or 1)
        tx_hash = str(item.get("hash") or "")
        incoming = recipient == focus

        transfers.append(
            Transfer(
                chain_id=chain_id,
                tx_hash=tx_hash,
                event_index=str(item.get("eventIndex") or ""),
                block_number=item.get("blockNumber"),
                block_time=item.get("timestamp"),
                direction="in" if incoming else "out",
                counterparty=sender if incoming else recipient,
                amount=format_amount(item.get("rawAmount"), asset.get("decimals")),
                asset=str(asset.get("symbol") or "unknown"),
                kind=str(item.get("kind") or ""),
                provider=str(item.get("source") or ""),
                source_url=explorer_url(chain_id, tx_hash),
            )
        )

    coverage = [
        Coverage(
            provider=str(entry.get("provider") or ""),
            status=str(entry.get("status") or "unknown"),
            pages=int(entry.get("pagesLoaded") or 0),
            chain_id=int(entry.get("chainId") or 0),
            address=str(entry.get("address") or ""),
        )
        for entry in payload.get("coverage") or []
    ]

    if not transfers and not coverage:
        raise FlowLensError(f"{case_path.name} has neither transfers nor coverage")

    return FlowLensCase(
        address=focus,
        mode=payload.get("mode"),
        updated_at=payload.get("updatedAt"),
        transfers=transfers,
        coverage=coverage,
        skipped=skipped,
    )


def plain_decimal(value: Decimal) -> str:
    """Format a Decimal without exponent notation or trailing zeros."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def group_amount(value: object) -> str:
    """Add thousands separators to the integer part for display only."""
    text = str(value)
    whole, dot, fraction = text.partition(".")
    sign = "-" if whole.startswith("-") else ""
    digits = whole[len(sign) :]
    if not digits.isdigit():
        return text
    grouped = f"{int(digits):,}"
    return f"{sign}{grouped}{dot}{fraction}" if dot else f"{sign}{grouped}"


def summarise(case: FlowLensCase) -> list[dict]:
    """Per-counterparty, per-asset totals, largest volume first."""
    groups: dict[tuple[str, str], dict] = {}
    for transfer in case.transfers:
        key = (transfer.counterparty, transfer.asset)
        row = groups.setdefault(key, {"count": 0, "in": Decimal(0), "out": Decimal(0)})
        row["count"] += 1
        try:
            amount = Decimal(transfer.amount)
        except (InvalidOperation, ValueError):
            continue
        row["in" if transfer.direction == "in" else "out"] += amount

    summary = [
        {
            "address": address,
            "asset": asset,
            "count": row["count"],
            "in": plain_decimal(row["in"]),
            "out": plain_decimal(row["out"]),
            "volume": row["in"] + row["out"],
        }
        for (address, asset), row in groups.items()
    ]
    # Ranking by raw volume would compare token units across assets, so an
    # airdrop with a huge supply would outrank real money movement.
    summary.sort(key=lambda item: (-item["count"], -item["volume"], item["address"]))
    return summary
