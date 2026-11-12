"""Dashboard output: escaping, self-containment, key figures."""

from __future__ import annotations

from decimal import Decimal

from tests.factories import make_record, make_result
from wallet_analyzer.dashboard import render_dashboard, render_document, write_dashboard
from wallet_analyzer.models import ActivityStatus, Network, VerificationStatus


def test_dashboard_reports_the_headline_numbers(sample_result) -> None:
    html = render_dashboard(sample_result)

    assert "Addresses analysed" in html
    assert "Matching wallets" in html
    assert "89,529.60" in html  # the single match value
    assert "CoinGecko simple/price" in html
    assert "BEP-20 value" in html
    assert "Token USD" in html
    assert "unlisted tokens" in html


def test_dashboard_escapes_untrusted_text() -> None:
    record = make_record(
        address="0xabc",
        notes=['<script>alert("x")</script>'],
        activity=ActivityStatus.UNKNOWN,
        verification=VerificationStatus.PARTIAL,
    )
    html = render_dashboard(make_result([record]))

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_dashboard_is_self_contained(sample_result) -> None:
    html = render_dashboard(sample_result)

    assert "<link" not in html.lower()
    assert "src=\"http" not in html.lower()
    assert "@import" not in html.lower()
    assert "<style>" in html
    assert "<script>" in html


def test_document_wraps_fragment_with_doctype(sample_result) -> None:
    document = render_document(sample_result)
    assert document.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in document
    assert document.rstrip().endswith("</html>")


def test_empty_matches_explains_itself() -> None:
    record = make_record(
        activity=ActivityStatus.INACTIVE, verification=VerificationStatus.VERIFIED
    )
    html = render_dashboard(make_result([record], matches=[]))
    assert "No wallet met every criterion" in html


def test_balance_range_and_window_are_shown(sample_result) -> None:
    html = render_dashboard(sample_result)
    assert "50,000" in html and "900,000" in html
    assert "activity within 30 days" in html


def test_dashboard_writes_file(sample_result, tmp_path) -> None:
    path = write_dashboard(
        sample_result, tmp_path / "d.html", provider_modes={Network.BITCOIN: "blockchain.com"}
    )
    assert path.exists()
    assert "blockchain.com" in path.read_text(encoding="utf-8")


def test_zero_usd_value_renders_as_zero_not_dash() -> None:
    record = make_record(usd_value=Decimal("0"), balance=Decimal("0"))
    html = render_dashboard(make_result([record], matches=[record]))
    assert "$0.00" in html or "0.00" in html
