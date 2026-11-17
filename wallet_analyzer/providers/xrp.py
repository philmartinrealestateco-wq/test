"""XRP balances and activity from the keyless XRPScan API.

XRPScan's account endpoint returns the balance in drops, and its transactions
endpoint returns already-decoded transactions with ISO dates. Both are
read-only and need no key. Only classic ``r...`` accounts are supported; issued
tokens on the XRP Ledger are outside this tool's native-only valuation.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from ..models import Network
from ..net import HttpClient, ProviderError
from .base import ProviderResult

DROPS_PER_XRP = Decimal(10) ** 6
ACCOUNT_URL = "https://api.xrpscan.com/api/v1/account/{address}"
TRANSACTIONS_URL = "https://api.xrpscan.com/api/v1/account/{address}/transactions"
HOST = "api.xrpscan.com"
TX_LOOKBACK = 25


class XrpProvider:
    network = Network.XRP

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    def fetch(self, address: str) -> ProviderResult:
        account = self._client.get_json(
            ACCOUNT_URL.format(address=address),
            host=HOST,
        )
        if not isinstance(account, dict):
            raise ProviderError("XRPScan returned an unexpected account payload", host=HOST)

        balance = _balance_from(account)

        notes = ["Native XRP only; issued tokens are excluded"]
        try:
            history = self._client.get_json(
                TRANSACTIONS_URL.format(address=address),
                {"limit": TX_LOOKBACK},
                host=HOST,
            )
        except ProviderError as exc:
            notes.append(f"activity lookup failed: {exc}")
            return ProviderResult(
                balance=balance,
                balance_verified=True,
                activity_verified=False,
                balance_source="XRPScan account (api.xrpscan.com)",
                notes=notes,
            )

        if not isinstance(history, dict) or not isinstance(
            history.get("transactions"), list
        ):
            notes.append(
                "XRPScan returned an unexpected transaction payload; activity was not verified"
            )
            return ProviderResult(
                balance=balance,
                balance_verified=True,
                activity_verified=False,
                balance_source="XRPScan account (api.xrpscan.com)",
                notes=notes,
            )

        transactions = [
            transaction
            for transaction in history["transactions"]
            if isinstance(transaction, dict)
        ]
        last_tx_at = _newest_successful(transactions, notes)
        activity_verified = last_tx_at is not None

        if last_tx_at is None:
            if history.get("marker"):
                notes.append(
                    f"the newest {len(transactions)} transactions all failed; "
                    "older activity was not checked"
                )
            else:
                activity_verified = True
                if transactions:
                    notes.append("the returned transaction history contains no successful transaction")

        return ProviderResult(
            balance=balance,
            balance_verified=True,
            last_tx_at=last_tx_at,
            activity_verified=activity_verified,
            balance_source="XRPScan account (api.xrpscan.com)",
            activity_source=(
                "XRPScan transactions (api.xrpscan.com)" if activity_verified else ""
            ),
            notes=notes,
        )


def _balance_from(account: dict) -> Decimal:
    raw = account.get("Balance")
    if raw is not None:
        try:
            return Decimal(int(raw)) / DROPS_PER_XRP
        except (TypeError, ValueError, InvalidOperation) as error:
            raise ProviderError(
                "XRPScan returned a non-integer XRP balance",
                host=HOST,
            ) from error

    # XRPScan also exposes the human-readable amount; use it only as a fallback.
    fallback = account.get("xrpBalance")
    try:
        return Decimal(str(fallback))
    except (TypeError, ValueError, InvalidOperation) as error:
        raise ProviderError("XRPScan returned no usable XRP balance", host=HOST) from error


def _newest_successful(
    transactions: list[dict],
    notes: list[str],
) -> datetime | None:
    latest: datetime | None = None
    for transaction in transactions:
        if transaction.get("validated") is not True:
            continue
        meta = transaction.get("meta")
        if not isinstance(meta, dict) or meta.get("TransactionResult") != "tesSUCCESS":
            continue

        raw_date = transaction.get("date")
        try:
            parsed = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            notes.append("XRPScan returned a successful transaction without a usable date")
            continue

        if latest is None or parsed > latest:
            latest = parsed
    return latest
