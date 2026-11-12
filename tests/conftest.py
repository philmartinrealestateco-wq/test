"""Fixtures for the test suite."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.factories import make_record, make_result
from wallet_analyzer.models import ActivityStatus, Network, VerificationStatus


@pytest.fixture
def sample_result():
    return make_result(
        [
            make_record(
                address="bc1qmatched",
                network=Network.BITCOIN,
                balance=Decimal("1.2"),
                usd_value=Decimal("89529.6"),
                days_ago=1,
            ),
            make_record(
                address="0xstale",
                network=Network.ETHEREUM,
                balance=Decimal("100"),
                usd_value=Decimal("207826"),
                days_ago=91,
                activity=ActivityStatus.INACTIVE,
            ),
            make_record(
                address="0xpartial",
                network=Network.BSC,
                balance=Decimal("200"),
                usd_value=Decimal("137224"),
                days_ago=None,
                activity=ActivityStatus.UNKNOWN,
                verification=VerificationStatus.PARTIAL,
                notes=["activity requires a paid Etherscan plan"],
            ),
            make_record(
                address="0xinvalid",
                network=Network.BSC,
                balance=None,
                usd_value=None,
                days_ago=None,
                activity=ActivityStatus.UNKNOWN,
                verification=VerificationStatus.INVALID,
                notes=["bad checksum"],
            ),
        ]
    )
