"""Render out/master_targets.csv as a dark-mode HTML report."""

from __future__ import annotations

import argparse
import csv
import html
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "out" / "master_targets.csv"
DEFAULT_OUTPUT = ROOT / "out" / "targets_report.html"

EXPLORERS = {
    "Ethereum": ("Etherscan", "https://etherscan.io/address/{address}"),
    "BNB Smart Chain": ("BscScan", "https://bscscan.com/address/{address}"),
}

CSS = """
:root {
  color-scheme: dark;
  --bg: #0b0f14;
  --panel: #111821;
  --panel-2: #0d141d;
  --border: #223041;
  --text: #e6edf3;
  --muted: #93a1b1;
  --accent: #58c4ff;
  --green: #3fb950;
  --amber: #d29922;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: radial-gradient(1200px 600px at 20% -10%, #132031 0%, var(--bg) 55%);
  color: var(--text);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  min-height: 100vh;
}
main { max-width: 1200px; margin: 0 auto; padding: 48px 24px 64px; }
.eyebrow {
  text-transform: uppercase; letter-spacing: .18em; font-size: 11px;
  color: var(--accent); margin: 0 0 12px; font-weight: 700;
}
h1 { font-size: clamp(28px, 4vw, 44px); margin: 0 0 8px; letter-spacing: -.02em; }
.subtitle { color: var(--muted); margin: 0 0 26px; font-size: 14px; }
.stats { display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 28px; }
.stat {
  background: linear-gradient(180deg, var(--panel), var(--panel-2));
  border: 1px solid var(--border); border-radius: 12px;
  padding: 14px 18px; min-width: 138px;
}
.stat .value { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }
.stat .label {
  color: var(--muted); font-size: 11px; text-transform: uppercase;
  letter-spacing: .08em; margin-top: 2px;
}
.table-wrap {
  border: 1px solid var(--border); border-radius: 16px; overflow: auto;
  background: var(--panel); box-shadow: 0 18px 50px rgba(0, 0, 0, .35);
}
table { width: 100%; border-collapse: collapse; min-width: 940px; }
thead th {
  position: sticky; top: 0; z-index: 1; background: var(--panel-2);
  color: var(--muted); font-size: 11px; text-transform: uppercase;
  letter-spacing: .12em; text-align: left; padding: 14px 16px;
  border-bottom: 1px solid var(--border); white-space: nowrap;
}
thead th.usd { text-align: right; }
tbody td { padding: 14px 16px; border-bottom: 1px solid rgba(34, 48, 65, .6); vertical-align: middle; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: rgba(88, 196, 255, .05); }
.rank { color: var(--muted); font-variant-numeric: tabular-nums; width: 56px; }
.address {
  font-family: var(--mono); color: var(--accent); text-decoration: none;
  font-size: 13px; word-break: break-all;
}
.address:hover { text-decoration: underline; }
.usd {
  text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums;
  font-weight: 600; white-space: nowrap;
}
.badges { display: flex; flex-wrap: wrap; gap: 6px; }
.badge {
  display: inline-block; padding: 3px 9px; border-radius: 999px;
  font-size: 11px; border: 1px solid var(--border); color: var(--muted);
  background: var(--panel-2); white-space: nowrap; text-decoration: none;
}
.badge.net { color: #b8d7ea; border-color: #24455c; background: #0e1b26; }
.badge.net:hover { border-color: var(--accent); color: var(--accent); }
.badge.token { color: #d7c7ff; border-color: #3b2f5c; background: #171226; }
.status {
  display: inline-block; font-size: 10px; font-weight: 700; letter-spacing: .06em;
  padding: 4px 9px; border-radius: 999px;
}
.status.verified { color: var(--green); background: rgba(63, 185, 80, .12); border: 1px solid rgba(63, 185, 80, .35); }
.status.partial { color: var(--amber); background: rgba(210, 153, 34, .12); border: 1px solid rgba(210, 153, 34, .35); }
footer { color: var(--muted); font-size: 12px; margin-top: 18px; line-height: 1.7; }
footer code { font-family: var(--mono); color: #b8c7d6; }
@media (max-width: 640px) {
  main { padding: 28px 14px 48px; }
  .stat { flex: 1 1 40%; }
}
"""


def load_entries(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise SystemExit(f"input not found: {path}")

    entries: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                total = Decimal(row["Best Total USD"])
            except (KeyError, InvalidOperation):
                continue
            entries.append(
                {
                    "wallet": row.get("Wallet", ""),
                    "tokens": [t for t in row.get("Tokens", "").split(",") if t],
                    "networks": [n for n in row.get("Networks", "").split(",") if n],
                    "total": total,
                    "status": row.get("Status", ""),
                }
            )

    if not entries:
        raise SystemExit(f"no wallets found in {path}")
    entries.sort(key=lambda entry: entry["total"], reverse=True)  # type: ignore[arg-type,call-overload]
    return entries


def _explorer_links(address: str, networks: list[str]) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for network in networks:
        explorer = EXPLORERS.get(network)
        if explorer is None:
            continue
        label, template = explorer
        links.append((label, template.format(address=address)))
    return links


def _badges(values: list[str], css_class: str) -> str:
    if not values:
        return '<span class="badge">—</span>'
    return "".join(
        f'<span class="badge {css_class}">{html.escape(value)}</span>' for value in values
    )


def _network_badges(networks: list[str], address: str) -> str:
    if not networks:
        return '<span class="badge">—</span>'
    badges: list[str] = []
    for network in networks:
        explorer = EXPLORERS.get(network)
        if explorer is None:
            badges.append(f'<span class="badge net">{html.escape(network)}</span>')
            continue
        label, template = explorer
        url = html.escape(template.format(address=address))
        badges.append(
            f'<a class="badge net" href="{url}" target="_blank" '
            f'rel="noopener noreferrer" title="Open on {label}">'
            f"{html.escape(network)}</a>"
        )
    return "".join(badges)


def render(entries: list[dict[str, object]], source: Path) -> str:
    verified = sum(1 for entry in entries if entry["status"] == "VERIFIED")
    partial = sum(1 for entry in entries if entry["status"] == "PARTIAL")
    eth_legs = sum(1 for entry in entries if "Ethereum" in entry["networks"])  # type: ignore[operator]
    bsc_legs = sum(1 for entry in entries if "BNB Smart Chain" in entry["networks"])  # type: ignore[operator]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows: list[str] = []
    for index, entry in enumerate(entries, start=1):
        address = str(entry["wallet"])
        networks = list(entry["networks"])  # type: ignore[arg-type]
        links = _explorer_links(address, networks)
        primary = links[0][1] if links else None
        if primary is not None:
            address_html = (
                f'<a class="address" href="{html.escape(primary)}" target="_blank" '
                f'rel="noopener noreferrer">{html.escape(address)}</a>'
            )
        else:
            address_html = f'<span class="address">{html.escape(address)}</span>'

        status = str(entry["status"])
        status_class = status.lower() if status in {"VERIFIED", "PARTIAL"} else ""
        rows.append(
            "<tr>"
            f'<td class="rank">{index}</td>'
            f"<td>{address_html}</td>"
            f'<td class="usd">${entry["total"]:,.2f}</td>'  # type: ignore[call-overload]
            f'<td><div class="badges">{_network_badges(networks, address)}</div></td>'
            f'<td><div class="badges">{_badges(list(entry["tokens"]), "token")}</div></td>'  # type: ignore[arg-type]
            f'<td><span class="status {status_class}">{html.escape(status)}</span></td>'
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Target Wallets · $50k–$900k</title>
<style>{CSS}</style>
</head>
<body>
<main>
  <p class="eyebrow">wallet-analyzer</p>
  <h1>Target Wallets</h1>
  <p class="subtitle">{len(entries)} unique wallets in the $50,000–$900,000 window · generated {generated}</p>

  <section class="stats">
    <div class="stat"><div class="value">{len(entries)}</div><div class="label">Unique wallets</div></div>
    <div class="stat"><div class="value">{verified}</div><div class="label">Verified</div></div>
    <div class="stat"><div class="value">{partial}</div><div class="label">Partial</div></div>
    <div class="stat"><div class="value">{eth_legs}</div><div class="label">Ethereum legs</div></div>
    <div class="stat"><div class="value">{bsc_legs}</div><div class="label">BSC legs</div></div>
  </section>

  <section class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Wallet</th>
          <th class="usd">Best Total USD</th>
          <th>Networks</th>
          <th>Tokens</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {"".join(rows)}
      </tbody>
    </table>
  </section>

  <footer>
    Sorted by Best Total USD, highest first. Addresses link to Etherscan or BscScan
    according to the networks that matched.<br>
    Best Total USD is native coin plus BEP-20 allowlist value — not the stablecoin
    balance of the token whose holder list surfaced the wallet.<br>
    Source: <code>{html.escape(str(source))}</code>
  </footer>
</main>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="master CSV")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="HTML output")
    args = parser.parse_args()

    entries = load_entries(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(entries, args.input), encoding="utf-8", newline="\n")

    print(f"wallets: {len(entries)}")
    print(f"written: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
