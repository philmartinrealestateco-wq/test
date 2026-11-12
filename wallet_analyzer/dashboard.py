"""A single-file HTML dashboard: no CDN, no build step, works offline."""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from string import Template

from .models import (
    ActivityStatus,
    AnalysisResult,
    Network,
    VerificationStatus,
    WalletRecord,
)
from .report import format_amount, format_timestamp, format_usd

_CHIP_CLASS = {
    VerificationStatus.VERIFIED: "ok",
    VerificationStatus.PARTIAL: "warn",
    VerificationStatus.UNVERIFIED: "bad",
    VerificationStatus.INVALID: "bad",
    ActivityStatus.ACTIVE: "ok",
    ActivityStatus.INACTIVE: "muted",
    ActivityStatus.UNKNOWN: "warn",
}


def render_dashboard(
    result: AnalysisResult,
    provider_modes: dict[Network, str] | None = None,
) -> str:
    modes = provider_modes or {}
    return _TEMPLATE.substitute(
        title="Wallet Balance Analysis",
        generated=format_timestamp(result.finished_at),
        duration=f"{(result.finished_at - result.started_at).total_seconds():.1f}s",
        range_text=(
            f"${result.settings.min_usd:,.0f} – ${result.settings.max_usd:,.0f} USD"
            f" · activity within {result.settings.activity_days} days"
        ),
        kpis=_render_kpis(result),
        distribution=_render_distribution(result),
        prices=_render_prices(result),
        matches_table=_render_matches_table(result.matches),
        matches_count=len(result.matches),
        network_options=_render_network_options(result.matches),
        quality=_render_quality(result),
        quality_count=result.total_addresses - result.verified_count,
        providers=_render_providers(result, modes),
        price_source=escape(result.price_source),
        price_time=format_timestamp(result.price_fetched_at),
        total=result.total_addresses,
    )


def write_dashboard(
    result: AnalysisResult,
    path: Path,
    *,
    provider_modes: dict[Network, str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_document(result, provider_modes), encoding="utf-8")
    return path


def render_document(
    result: AnalysisResult,
    provider_modes: dict[Network, str] | None = None,
) -> str:
    """Standalone HTML document, suitable for opening straight from disk."""

    body = render_dashboard(result, provider_modes)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Wallet Balance Analysis</title>\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


# -- fragments -----------------------------------------------------------


def _render_kpis(result: AnalysisResult) -> str:
    cards = [
        ("Addresses analysed", f"{result.total_addresses:,}", "unique wallets checked"),
        ("Fully verified", f"{result.verified_count:,}", "balance and activity confirmed"),
        ("Active wallets", f"{result.active_count:,}", "confirmed tx inside the window"),
        ("In balance range", f"{result.in_range_count:,}", "valued inside the thresholds"),
        ("Matching wallets", f"{len(result.matches):,}", "verified, active, in range"),
        ("Matched value", f"${result.matched_total_usd:,.0f}", "total USD across matches"),
        ("BEP-20 value", f"${result.token_value_usd:,.0f}", "priced token holdings (BSC allowlist)"),
    ]
    return "".join(
        f'<div class="kpi"><span class="kpi-label">{escape(label)}</span>'
        f'<span class="kpi-value">{escape(value)}</span>'
        f'<span class="kpi-hint">{escape(hint)}</span></div>'
        for label, value, hint in cards
    )


def _render_distribution(result: AnalysisResult) -> str:
    distribution = result.network_distribution()
    if not distribution:
        return '<p class="empty">No wallets analysed.</p>'

    peak = max(distribution.values())
    rows = []
    for network in Network:
        count = distribution.get(network, 0)
        if not count:
            continue
        share = 100 * count / peak
        matches = sum(1 for record in result.matches if record.network is network)
        rows.append(
            '<div class="bar-row">'
            f'<span class="bar-label">{escape(network.label)}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{share:.1f}%"></span></span>'
            f'<span class="bar-count">{count}</span>'
            f'<span class="bar-match">{matches} matched</span>'
            "</div>"
        )
    return "".join(rows)


def _render_prices(result: AnalysisResult) -> str:
    rows = "".join(
        f"<tr><td>{escape(asset)}</td><td class=\"num\">${price:,.2f}</td></tr>"
        for asset, price in result.prices.items()
    )
    return (
        "<table class=\"mini\"><thead><tr><th>Asset</th><th class=\"num\">USD</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f'<p class="note">Source: {escape(result.price_source)} · '
        f"{escape(format_timestamp(result.price_fetched_at) or 'unknown time')}</p>"
    )


def _render_matches_table(matches: list[WalletRecord]) -> str:
    if not matches:
        return '<tr><td colspan="11" class="empty-cell">No wallet met every criterion in this run.</td></tr>'

    rows = []
    for record in matches:
        link = escape(record.network.explorer_url(record.address), quote=True)
        rows.append(
            f'<tr data-usd="{_num(record.total_usd)}" data-days="{record.days_since_last_tx if record.days_since_last_tx is not None else 99999}">'
            f'<td class="mono"><a href="{link}" target="_blank" rel="noopener noreferrer">{escape(record.address)}</a></td>'
            f"<td>{escape(record.network.label)}</td>"
            f"<td>{escape(record.native_symbol)}</td>"
            f'<td class="num">{escape(format_amount(record.balance))}</td>'
            f'<td class="num">{escape(format_usd(record.usd_value))}</td>'
            f'<td class="num">{escape(format_usd(record.tokens_usd) or "—")}</td>'
            f'<td class="num strong">{escape(format_usd(record.total_usd))}</td>'
            f"<td>{escape(format_timestamp(record.last_tx_at) or '—')}</td>"
            f'<td class="num">{record.days_since_last_tx if record.days_since_last_tx is not None else "—"}</td>'
            f'<td><span class="chip ok">ACTIVE</span></td>'
            f'<td class="source">{escape(record.data_source)}</td>'
            "</tr>"
        )
    return "".join(rows)


def _render_network_options(matches: list[WalletRecord]) -> str:
    networks = sorted({record.network.label for record in matches})
    return "".join(f'<option value="{escape(label)}">{escape(label)}</option>' for label in networks)


def _render_quality(result: AnalysisResult) -> str:
    problems = [
        record
        for record in result.records
        if record.verification_status is not VerificationStatus.VERIFIED
    ]
    if not problems:
        return '<p class="empty">Every address was fully verified against a chain-backed source.</p>'

    rows = []
    for record in problems[:60]:
        link = escape(record.network.explorer_url(record.address), quote=True)
        verification = record.verification_status.value
        activity = record.activity_status.value
        rows.append(
            "<tr>"
            f'<td class="mono"><a href="{link}" target="_blank" rel="noopener noreferrer">{escape(_short(record.address))}</a></td>'
            f"<td>{escape(record.network.label)}</td>"
            f'<td><span class="chip {_CHIP_CLASS[record.verification_status]}">{escape(verification)}</span></td>'
            f'<td><span class="chip {_CHIP_CLASS[record.activity_status]}">{escape(activity)}</span></td>'
            f'<td class="num">{escape(format_usd(record.total_usd) or "—")}</td>'
            f'<td class="source">{escape(record.joined_notes() or "no reason recorded")}</td>'
            "</tr>"
        )

    hidden = len(problems) - len(rows)
    footer = (
        f'<p class="note">Showing the first {len(rows)} of {len(problems)} not-fully-verified wallets'
        f" ({hidden} more in the CSV export).</p>"
        if hidden
        else ""
    )
    return (
        '<table class="mini"><thead><tr><th>Address</th><th>Network</th><th>Verification</th>'
        '<th>Activity</th><th class="num">USD</th><th>Reason</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>{footer}"
    )


def _render_providers(result: AnalysisResult, modes: dict[Network, str]) -> str:
    if not modes:
        return ""
    items = "".join(
        f"<li><strong>{escape(network.label)}</strong>: {escape(mode)}</li>"
        for network, mode in modes.items()
        if network in result.network_distribution()
    )
    return f'<ul class="providers">{items}</ul>'


def _short(address: str) -> str:
    return address if len(address) <= 18 else f"{address[:10]}…{address[-6:]}"


def _num(value: Decimal | None) -> str:
    return "0" if value is None else f"{value:.2f}"


_TEMPLATE = Template(
    """<div class="wrap">
  <header class="top">
    <div>
      <h1>$title</h1>
      <p class="sub">Generated $generated · finished in $duration · $range_text</p>
    </div>
    <div class="stamp">read-only analysis<br><span>$total addresses</span></div>
  </header>

  <section class="kpis">$kpis</section>

  <section class="grid2">
    <div class="panel">
      <h2>Network distribution</h2>
      $distribution
    </div>
    <div class="panel">
      <h2>Price snapshot</h2>
      $prices
    </div>
  </section>

  <section class="panel">
    <div class="panel-head">
      <h2>Matching wallets <span class="count">$matches_count</span></h2>
      <div class="controls">
        <input id="q" type="search" placeholder="Search address or source" aria-label="Search matches">
        <select id="net" aria-label="Filter by network">
          <option value="">All networks</option>
          $network_options
        </select>
      </div>
    </div>
    <div class="table-scroll">
      <table id="matches">
        <thead>
          <tr>
            <th data-type="text">Wallet address</th>
            <th data-type="text">Network</th>
            <th data-type="text">Token</th>
            <th data-type="num" class="num">Balance</th>
            <th data-type="num" class="num">Native USD</th>
            <th data-type="num" class="num">Token USD</th>
            <th data-type="num" class="num">Total USD</th>
            <th data-type="text">Last transaction</th>
            <th data-type="num" class="num">Days</th>
            <th data-type="text">Activity</th>
            <th data-type="text">Data source</th>
          </tr>
        </thead>
        <tbody>$matches_table</tbody>
      </table>
    </div>
  </section>

  <details class="panel">
    <summary>Data quality — $quality_count wallet(s) not fully verified</summary>
    <div class="table-scroll">$quality</div>
  </details>

  <footer>
    <p>Thresholds: $range_text. Prices: $price_source, $price_time.
    Every balance is read from a public chain API; nothing in this report is estimated.
    BEP-20 values cover a configured contract allowlist, not every token a wallet
    holds: unlisted tokens and full history require an indexer.</p>
    $providers
    <p class="fine">Generated by wallet-analyzer. Read-only: no keys, seed phrases or signing capability are used anywhere in this tool.</p>
  </footer>
</div>

<style>
  :root {
    --bg: #f4f5f7;
    --panel: #ffffff;
    --ink: #111827;
    --muted: #6b7280;
    --line: #e5e7eb;
    --accent: #1d4ed8;
    --ok: #047857;
    --ok-bg: #ecfdf5;
    --warn: #b45309;
    --warn-bg: #fffbeb;
    --bad: #b91c1c;
    --bad-bg: #fef2f2;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font: 14px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  .wrap { max-width: 1240px; margin: 0 auto; padding: 28px 20px 48px; }
  h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
  h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin: 0 0 14px; }
  .sub, .note, .fine { color: var(--muted); font-size: 12px; margin: 0; }
  .top { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 22px; }
  .stamp { text-align: right; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
  .stamp span { color: var(--ink); font-weight: 600; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; display: flex; flex-direction: column; gap: 2px; }
  .kpi-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
  .kpi-value { font-size: 24px; font-weight: 650; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
  .kpi-hint { font-size: 11px; color: var(--muted); }
  .grid2 { display: grid; grid-template-columns: 1.15fr 1fr; gap: 16px; margin-bottom: 20px; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 18px; margin-bottom: 20px; }
  .grid2 .panel { margin-bottom: 0; }
  .panel-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
  .panel-head h2 { margin: 0; }
  .count { background: #eef2ff; color: var(--accent); border-radius: 999px; padding: 1px 8px; font-size: 12px; letter-spacing: 0; }
  .controls { display: flex; gap: 8px; flex-wrap: wrap; }
  input[type="search"], select {
    font: inherit; font-size: 13px; padding: 7px 10px; border: 1px solid var(--line);
    border-radius: 7px; background: #fbfbfc; color: var(--ink); min-width: 180px;
  }
  input[type="search"]:focus, select:focus { outline: 2px solid #c7d2fe; border-color: var(--accent); }
  .table-scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th {
    text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--muted); padding: 8px 10px; border-bottom: 1px solid var(--line); white-space: nowrap;
  }
  th[data-type] { cursor: pointer; user-select: none; }
  th[data-type]:hover { color: var(--ink); }
  th.sorted-asc::after { content: " ↑"; }
  th.sorted-desc::after { content: " ↓"; }
  td { padding: 9px 10px; border-bottom: 1px solid #f1f2f4; vertical-align: top; }
  tbody tr:hover { background: #fafafa; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .strong { font-weight: 600; }
  .mono { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 12px; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .source { color: var(--muted); font-size: 12px; max-width: 320px; }
  .chip { display: inline-block; font-size: 10px; font-weight: 650; letter-spacing: 0.05em; border-radius: 999px; padding: 2px 7px; }
  .chip.ok { background: var(--ok-bg); color: var(--ok); }
  .chip.warn { background: var(--warn-bg); color: var(--warn); }
  .chip.bad { background: var(--bad-bg); color: var(--bad); }
  .chip.muted { background: #f3f4f6; color: var(--muted); }
  .bar-row { display: grid; grid-template-columns: 132px 1fr 44px 84px; align-items: center; gap: 10px; margin-bottom: 9px; font-size: 12.5px; }
  .bar-label { color: var(--ink); }
  .bar-track { background: #f1f2f4; border-radius: 999px; height: 8px; overflow: hidden; }
  .bar-fill { display: block; height: 100%; background: linear-gradient(90deg, #93b4ff, var(--accent)); border-radius: 999px; }
  .bar-count { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }
  .bar-match { color: var(--muted); font-size: 11.5px; }
  table.mini th, table.mini td { padding: 7px 10px; }
  .empty, .empty-cell { color: var(--muted); font-style: italic; }
  .empty-cell { text-align: center; padding: 22px; }
  details summary { cursor: pointer; font-size: 13px; font-weight: 600; }
  details summary::marker { color: var(--muted); }
  details[open] summary { margin-bottom: 14px; }
  footer { border-top: 1px solid var(--line); padding-top: 16px; color: var(--muted); font-size: 12px; }
  footer p { margin: 0 0 8px; }
  ul.providers { margin: 8px 0; padding-left: 18px; }
  ul.providers li { margin-bottom: 2px; }
  .fine { font-size: 11px; }
  @media (max-width: 860px) {
    .grid2 { grid-template-columns: 1fr; }
    .top { flex-direction: column; }
    .stamp { text-align: left; }
  }
  @media print {
    body { background: #fff; }
    .controls { display: none; }
    .panel { break-inside: avoid; }
  }
</style>

<script>
  (function () {
    var table = document.getElementById("matches");
    if (!table) return;
    var body = table.tBodies[0];
    var search = document.getElementById("q");
    var network = document.getElementById("net");

    function rows() {
      return Array.prototype.slice.call(body.rows);
    }

    function applyFilters() {
      var term = (search.value || "").toLowerCase();
      var chain = network.value;
      rows().forEach(function (row) {
        var text = row.textContent.toLowerCase();
        var matchesTerm = !term || text.indexOf(term) !== -1;
        var matchesChain = !chain || row.cells[1].textContent.trim() === chain;
        row.style.display = matchesTerm && matchesChain ? "" : "none";
      });
    }

    search.addEventListener("input", applyFilters);
    network.addEventListener("change", applyFilters);

    Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th, index) {
      var kind = th.getAttribute("data-type");
      if (!kind) return;
      var ascending = false;
      th.addEventListener("click", function () {
        Array.prototype.forEach.call(table.tHead.rows[0].cells, function (other) {
          other.classList.remove("sorted-asc", "sorted-desc");
        });
        ascending = !ascending;
        th.classList.add(ascending ? "sorted-asc" : "sorted-desc");
        var sorted = rows().sort(function (a, b) {
          var left = a.cells[index].textContent.trim();
          var right = b.cells[index].textContent.trim();
          if (kind === "num") {
            left = parseFloat(left.replace(/[^0-9.-]/g, "")) || 0;
            right = parseFloat(right.replace(/[^0-9.-]/g, "")) || 0;
          }
          if (left < right) return ascending ? -1 : 1;
          if (left > right) return ascending ? 1 : -1;
          return 0;
        });
        sorted.forEach(function (row) { body.appendChild(row); });
      });
    });
  })();
</script>
"""
)
