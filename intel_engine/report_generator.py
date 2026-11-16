"""Phase 4: render one wallet's intel into a styled PDF report.

Draws everything from ``intel.db``: the ENS cache, the OpenSanctions screening
result, the web mentions collected in Phase 2 and the sherlock/holehe results
from Phase 3. The Markdown is the source of truth, converted to HTML with
``markdown`` and printed with ``xhtml2pdf``.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path

import markdown
from xhtml2pdf import pisa

from database import DB_PATH, init_db

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
DEFAULT_ADDRESS = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
DEFAULT_OUTPUT = REPORTS_DIR / "vitalik_test.pdf"

STYLE = """
@page {
    size: A4;
    margin: 1.7cm 1.5cm 1.9cm 1.5cm;
    @bottom-right { content: "Page " counter(page) " of " counter(pages); font-size: 8pt; color: #8a949e; }
    @bottom-left { content: "intel_engine"; font-size: 8pt; color: #8a949e; }
}
body { font-family: Helvetica, Arial, sans-serif; font-size: 9.5pt; color: #1f2328; line-height: 1.45; }
h1 { font-size: 19pt; color: #0b3d5c; margin: 0 0 2pt 0; }
h2 { font-size: 12.5pt; color: #0b3d5c; border-bottom: 1pt solid #cfe0ec; padding-bottom: 3pt; margin-top: 16pt; }
p { margin: 4pt 0; }
table { width: 100%; margin: 5pt 0 9pt 0; }
th { background-color: #0b3d5c; color: #ffffff; text-align: left; padding: 4pt 5pt; font-size: 8.5pt; }
td { border-bottom: 0.5pt solid #d8dee4; padding: 3.5pt 5pt; vertical-align: top; }
code { font-family: Courier, monospace; font-size: 8.5pt; color: #0b3d5c; }
.meta { color: #57606a; font-size: 9pt; }
.lede { color: #57606a; font-size: 9pt; margin-bottom: 8pt; }
.alert { background-color: #fdecea; border: 1pt solid #c62828; color: #7f1d1d; font-size: 9pt; padding: 6pt 8pt; margin: 6pt 0 8pt 0; }
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def cell(value: object) -> str:
    """Escape a Markdown table cell."""
    if value is None or value == "":
        return "&mdash;"
    return str(value).replace("|", "\\|").replace("\n", " ")


def load_wallet(db, address: str):
    return db.execute(
        "SELECT address, ens_name, ens_twitter, ens_email, last_scanned FROM wallets WHERE lower(address) = ?",
        (address.lower(),),
    ).fetchone()


def load_findings(db, address: str, tool: str) -> list:
    return db.execute(
        """
        SELECT finding, url, confidence, timestamp
        FROM osint_findings
        WHERE lower(address) = ? AND tool = ?
        ORDER BY finding
        """,
        (address.lower(), tool),
    ).fetchall()


def load_sanctions_runs(db) -> list:
    """Latest screening run per dataset, so the PDF states what was checked."""
    return db.execute(
        """
        SELECT dataset, entities, wallets, checked, matches, dataset_updated, checked_at
        FROM sanctions_runs AS runs
        WHERE rowid = (
            SELECT rowid FROM sanctions_runs
            WHERE dataset = runs.dataset
            ORDER BY checked_at DESC, rowid DESC
            LIMIT 1
        )
        ORDER BY dataset
        """
    ).fetchall()


def build_markdown(db, address: str) -> str:
    wallet = load_wallet(db, address)
    if wallet is None:
        raise SystemExit(f"error: {address} is not in {DB_PATH}; run the scraper first")

    web = load_findings(db, address, "DuckDuckGo")
    sherlock = load_findings(db, address, "Sherlock")
    holehe = load_findings(db, address, "Holehe")
    ens_records = load_findings(db, address, "ENS")
    sanctions = load_findings(db, address, "OpenSanctions")
    sanction_runs = load_sanctions_runs(db)

    ens_url = next((row["url"] for row in ens_records if row["url"]), None)
    holehe_blocked = any(row["confidence"] == "ERROR" for row in holehe)
    subject = wallet["ens_name"] or wallet["address"]

    lines: list[str] = [
        "# OSINT Target Report",
        "",
        f'<p class="lede">Off-chain exposure review for <code>{wallet["address"]}</code> '
        f'({subject}). Every value below comes from <code>intel.db</code>; nothing was '
        "re-verified for this document.</p>",
        "",
        f'<p class="meta">Generated {utc_now()} &middot; sources: ENS, DuckDuckGo, sherlock, holehe, OpenSanctions</p>',
        "",
        "## Target Overview",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Address | `{wallet['address']}` |",
        f"| ENS name | {cell(wallet['ens_name'])} |",
        f"| Twitter (ENS) | {cell('@' + wallet['ens_twitter'] if wallet['ens_twitter'] else None)} |",
        f"| Email (ENS) | {cell(wallet['ens_email'])} |",
        f"| Published URL (ENS) | {cell(ens_url)} |",
        f"| Last scanned | {cell(wallet['last_scanned'])} |",
        f"| Sanctions matches | {len(sanctions)} |",
        f"| Web mentions stored | {len(web)} |",
        f"| Sherlock accounts stored | {len(sherlock)} |",
        f"| Holehe rows stored | {len(holehe)} |",
        "",
    ]

    lines += ["## Sanctions Screening", ""]
    if sanctions:
        lines += [
            '<p class="alert"><strong>Listed wallet.</strong> This address appears in a '
            "sanctions dataset. A listing identifies the address, not the person or "
            "organisation that controls it.</p>",
            "",
            "| Match | Dataset entry |",
            "| --- | --- |",
        ]
        for row in sanctions:
            url = row["url"] or ""
            link = f"[{cell(url)}]({url})" if url else "&mdash;"
            lines.append(f"| {cell(row['finding'])} | {link} |")
        lines.append("")
    else:
        lines += ["_No sanctions listing matched this address._", ""]

    for run in sanction_runs:
        lines.append(
            f'<p class="lede">Screened {run["checked"]} cached wallet(s) against '
            f'{cell(run["dataset"])} &mdash; {run["wallets"]:,} sanctioned addresses of '
            f'{run["entities"]:,} entities, dataset updated {cell(run["dataset_updated"])} &mdash; '
            f'on {cell(run["checked_at"])}: {run["matches"]} match(es) in that run.</p>'
        )
    if not sanction_runs:
        lines += ["_No sanctions screening has been recorded for this database yet._"]
    lines += [
        "",
        '<p class="lede">Source: OpenSanctions, licensed CC BY-NC 4.0 '
        "(https://www.opensanctions.org/licensing/).</p>",
        "",
    ]

    lines += ["## Web Mentions", ""]
    if web:
        lines += ["| Title | URL | Confidence |", "| --- | --- | --- |"]
        for row in web:
            url = row["url"] or ""
            link = f"[{cell(url)}]({url})" if url else "&mdash;"
            lines.append(f"| {cell(row['finding'])} | {link} | {cell(row['confidence'])} |")
        lines.append("")
        lines.append(
            "<p class=\"lede\">Only results whose title, snippet or URL actually "
            "contained the address are listed; unrelated DuckDuckGo filler was discarded.</p>"
        )
    else:
        lines += ["_No web mentions recorded for this address._", ""]

    lines += ["## Sherlock Findings", ""]
    if sherlock:
        lines += ["| Platform | Profile URL | Confidence |", "| --- | --- | --- |"]
        for row in sherlock:
            url = row["url"] or ""
            link = f"[{cell(url)}]({url})" if url else "&mdash;"
            platform = row["finding"].removeprefix("Account on ")
            lines.append(f"| {cell(platform)} | {link} | {cell(row['confidence'])} |")
        lines.append("")
        lines += [
            f"<p class=\"lede\">{len(sherlock)} registration(s). Hits whose profile URL did "
            "not contain the handle were discarded as false positives.</p>",
            "",
        ]
    else:
        lines += ["_No sherlock findings recorded._", ""]

    lines += ["## Unresolved Questions", ""]
    questions: list[str] = []

    if wallet["ens_twitter"]:
        questions.append(
            f"The handle `@{wallet['ens_twitter']}` is a **self-asserted** ENS text record - "
            "anyone can set one. Ownership needs out-of-band proof (a signed message or the "
            "ENS transaction history)."
        )
    if wallet["ens_email"]:
        questions.append(
            f"The email `{wallet['ens_email']}` is likewise self-asserted; it is not proof of "
            "control."
        )
    else:
        questions.append(
            "No email pivot exists: the ENS record publishes no `email` text record, so holehe "
            "had nothing to enumerate."
        )
    if holehe_blocked:
        questions.append(
            "The email enumeration was rate-limited from this host (`IP Rate Limited`), so email "
            "exposure is **unknown**, not absent."
        )
    if sherlock:
        questions.append(
            f"None of the {len(sherlock)} sherlock registrations is cryptographically tied to this "
            "address - a username collision is more likely than one person controlling all of them."
        )
    if web:
        questions.append(
            "Web mentions are unverified search results: they establish that the address appears "
            "online, not who controls it."
        )
    if sanctions:
        questions.append(
            "The sanctions match concerns the address, not its operator: a designated wallet can "
            "be funded or drained by a third party, so control still needs separate proof."
        )
    elif sanction_runs:
        questions.append(
            "Sanctions screening returned no match; absence from a sanctions list is not evidence "
            "that the wallet is clean."
        )
    questions.append(
        "On-chain history (funding source, counterparties) was not reviewed here and is the "
        "next step for attribution."
    )

    lines += [f"- {question}" for question in questions]
    lines.append("")

    return "\n".join(lines)


def render_pdf(markdown_text: str, output_path: Path) -> Path:
    body = markdown.markdown(markdown_text, extensions=["tables", "sane_lists"])
    document = (
        "<html><head><meta charset=\"utf-8\" />"
        f"<style>{STYLE}</style></head><body>{body}</body></html>"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        status = pisa.CreatePDF(document, dest=handle, encoding="utf-8")

    if status.err:
        raise RuntimeError(f"xhtml2pdf reported {status.err} error(s); {output_path} is unusable")

    return output_path


def wallets_with_findings(db) -> list:
    """Wallets worth a PDF: an ENS name, or any finding that is not an ERROR row."""
    return db.execute(
        """
        SELECT w.address, w.ens_name
        FROM wallets w
        WHERE (w.ens_name IS NOT NULL AND TRIM(w.ens_name) <> '')
           OR EXISTS (
                SELECT 1 FROM osint_findings f
                WHERE lower(f.address) = lower(w.address)
                  AND (f.confidence IS NULL OR upper(f.confidence) <> 'ERROR')
           )
        ORDER BY lower(w.address)
        """
    ).fetchall()


def report_name(address: str, ens_name: str | None) -> str:
    """Human-friendly file stem: the ENS name when known, else the address."""
    stem = (ens_name or address).strip().lower()
    safe = re.sub(r"[^a-z0-9._-]+", "-", stem).strip("-.")
    return f"{safe or address}.pdf"


def run_batch(db, out_dir: Path = REPORTS_DIR) -> tuple[list[Path], int]:
    """Render one PDF per wallet with a finding; return (files, skipped count)."""
    eligible = wallets_with_findings(db)
    total = db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    generated: list[Path] = []

    for index, row in enumerate(eligible, start=1):
        markdown_text = build_markdown(db, row["address"])
        pdf = render_pdf(markdown_text, Path(out_dir) / report_name(row["address"], row["ens_name"]))
        generated.append(pdf)
        print(f"[{index}/{len(eligible)}] pdf: {pdf}")

    return generated, total - len(eligible)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--address", default=DEFAULT_ADDRESS, help="wallet to report on")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"PDF destination (default {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="render one PDF per wallet with at least one finding",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPORTS_DIR,
        help=f"directory for --all (default {REPORTS_DIR})",
    )
    args = parser.parse_args(argv)

    db = init_db()
    try:
        if args.all:
            generated, skipped = run_batch(db, args.out_dir)
        else:
            pdf = render_pdf(build_markdown(db, args.address), Path(args.output))
            generated, skipped = [pdf], 0
    finally:
        db.close()

    print(f"reports : {len(generated)} pdf")
    if args.all:
        print(f"skipped : {skipped} wallet(s) without findings")
    for pdf in generated:
        print(f"  {pdf} ({pdf.stat().st_size:,} bytes)")
    print(f"database: {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
