"""Phase 3: off-chain OSINT for wallets that already carry a pivot.

Finds wallets in ``intel.db`` with a Twitter handle or an email from Phase 2 and
runs the bundled CLIs against them: ``sherlock`` for the handle, ``holehe`` for
the email. Results go to ``osint_findings`` as ``UNVERIFIED``, since the tools
report third-party registrations that say nothing about who is behind them.

Rate limiting is reported, never hidden: holehe marks a blocked site ``[x]``, and
a run where every site is ``[x]`` is an error, not "no accounts".
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from database import DB_PATH, init_db
from onchain_scraper import store_findings, utc_now

SCRIPTS = Path(__file__).resolve().parent / ".venv" / "Scripts"
SHERLOCK = SCRIPTS / "sherlock.exe"
HOLEHE = SCRIPTS / "holehe.exe"

DEFAULT_TIMEOUT = 300
PER_REQUEST_TIMEOUT = "10"

TEST_ADDRESS = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
TEST_ENS = "vitalik.eth"
TEST_TWITTER = "VitalikButerin"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SHERLOCK_FOUND = re.compile(r"^\[\+\]\s*(?P<site>[^:]+):\s*(?P<url>\S+)\s*$")
_HOLEHE_LINE = re.compile(r"^\[(?P<mark>[+\-x])\]\s*(?P<site>\S.*?)\s*$")
_HOLEHE_CHECKED = re.compile(r"(?P<count>\d+)\s+websites checked")
_HOLEHE_LEGEND = "Email used"


@dataclass(frozen=True)
class ToolRun:
    name: str
    returncode: int | None
    output: str
    timed_out: bool = False
    error: str | None = None


@dataclass
class HoleheReport:
    used: list[str] = field(default_factory=list)
    not_used: list[str] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    checked: int | None = None


@dataclass(frozen=True)
class Pivot:
    address: str
    ens_name: str | None
    twitter: str | None
    email: str | None


def clean_output(text: str) -> str:
    """Strip ANSI colour and turn tqdm's carriage returns into line breaks."""
    return _ANSI.sub("", text).replace("\r", "\n")


def run_tool(name: str, command: list[str], timeout: int) -> ToolRun:
    """Run a CLI with a hard wall-clock limit, never raising on its failure."""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["NO_COLOR"] = "1"

    with tempfile.TemporaryDirectory() as workdir:
        try:
            completed = subprocess.run(
                command,
                cwd=workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired as expired:
            partial = expired.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            return ToolRun(name, None, partial, timed_out=True, error=f"timed out after {timeout}s")
        except OSError as error:
            return ToolRun(name, None, "", error=f"could not start: {error}")

    return ToolRun(name, completed.returncode, completed.stdout + completed.stderr)


def parse_sherlock(output: str) -> list[tuple[str, str]]:
    """Return ``(platform, profile_url)`` for every ``[+]`` line."""
    found: list[tuple[str, str]] = []
    for raw in clean_output(output).splitlines():
        match = _SHERLOCK_FOUND.match(raw.strip())
        if match:
            found.append((match["site"].strip(), match["url"]))
    return found


def parse_holehe(output: str) -> HoleheReport:
    """Count ``[+]`` used, ``[-]`` unused and ``[x]`` rate-limited sites."""
    report = HoleheReport()
    buckets = {
        "+": report.used,
        "-": report.not_used,
        "x": report.rate_limited,
    }

    for raw in clean_output(output).splitlines():
        match = _HOLEHE_LINE.match(raw.strip())
        if not match:
            continue
        site = match["site"].strip()
        if site.startswith(_HOLEHE_LEGEND):
            continue
        bucket = buckets[match["mark"]]
        if site not in bucket:  # holehe lists a few sites twice
            bucket.append(site)

    checked = _HOLEHE_CHECKED.search(output)
    if checked:
        report.checked = int(checked["count"])

    return report


def normalise_handle(value: str) -> str:
    """Turn ``@handle``, a profile URL or a bare handle into the handle."""
    handle = value.strip().rstrip("/")
    if "/" in handle:
        handle = handle.rsplit("/", 1)[-1]
    return handle.lstrip("@")


def filter_by_handle(handle: str, hits: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], int]:
    """Drop hits whose profile URL does not carry the handle.

    sherlock treats any 200 as a hit, so site roots such as
    ``https://discord.com`` come back as false positives; a real profile URL
    always contains the username.
    """
    needle = handle.lower()
    kept = [(site, url) for site, url in hits if needle in url.lower()]
    return kept, len(hits) - len(kept)


def load_pivots(db) -> list[Pivot]:
    rows = db.execute(
        """
        SELECT address, ens_name, ens_twitter, ens_email
        FROM wallets
        WHERE (ens_twitter IS NOT NULL AND TRIM(ens_twitter) <> '')
           OR (ens_email IS NOT NULL AND TRIM(ens_email) <> '')
        ORDER BY last_scanned DESC
        """
    ).fetchall()

    pivots: list[Pivot] = []
    for row in rows:
        twitter = normalise_handle(row["ens_twitter"]) if row["ens_twitter"] else None
        email = row["ens_email"].strip() if row["ens_email"] else None
        pivots.append(
            Pivot(address=row["address"], ens_name=row["ens_name"], twitter=twitter, email=email)
        )

    return pivots


def process_pivot(db, pivot: Pivot, timeout: int) -> tuple[int, int, int]:
    """Run every available tool for one wallet.

    Returns ``(findings stored, errors, warnings)`` - a warning is a recorded
    provider limitation such as holehe's IP rate limit.
    """
    label = pivot.ens_name or pivot.address
    print(f"{pivot.address}  ({label})")
    stored = 0
    errors = 0
    warned = 0

    if pivot.twitter:
        print(f"  sherlock : {pivot.twitter}  (limit {timeout}s)")
        run = run_tool(
            "Sherlock",
            [str(SHERLOCK), pivot.twitter, "--print-found", "--no-color", "--timeout", PER_REQUEST_TIMEOUT],
            timeout,
        )
        found = parse_sherlock(run.output)
        found, dropped = filter_by_handle(pivot.twitter, found)
        state = "timed out, partial" if run.timed_out else f"exit {run.returncode}"
        dropped_note = f"; dropped {dropped} hit(s) without the handle in the URL" if dropped else ""

        if run.error and not found:
            errors += 1
            print(f"  sherlock : ERROR — {run.error}")
        elif run.returncode not in (0, None) and not found and not run.timed_out:
            errors += 1
            print(f"  sherlock : ERROR — exit {run.returncode} with nothing to parse")
        elif not found:
            print(f"  sherlock : no accounts found ({state}{dropped_note})")
        else:
            store_findings(
                db,
                pivot.address,
                "Sherlock",
                [(f"Account on {site}", url) for site, url in found],
                "UNVERIFIED",
            )
            db.commit()
            stored += len(found)
            print(f"  sherlock : {len(found)} account(s) ({state}{dropped_note})")
            for site, url in found:
                print(f"             * {site}: {url}")

    if pivot.email:
        print(f"  holehe   : {pivot.email}  (limit {timeout}s)")
        run = run_tool(
            "Holehe",
            [str(HOLEHE), pivot.email, "--no-color", "--no-clear", "--timeout", PER_REQUEST_TIMEOUT],
            timeout,
        )
        report = parse_holehe(run.output)

        if run.error and report.checked is None:
            errors += 1
            print(f"  holehe   : ERROR — {run.error}")
        elif report.checked is None:
            errors += 1
            print(f"  holehe   : ERROR — could not parse a site count (exit {run.returncode})")
        else:
            blocked = len(report.rate_limited)
            if report.used:
                store_findings(
                    db,
                    pivot.address,
                    "Holehe",
                    [(f"Account on {site}", None) for site in report.used],
                    "UNVERIFIED",
                )
                db.commit()
                stored += len(report.used)
                print(
                    f"  holehe   : {len(report.used)} used of {report.checked} sites "
                    f"({blocked} rate-limited)"
                )
                for site in report.used:
                    print(f"             * {site}")
            elif blocked:
                # A throttled provider is a fact about the run, not a failed
                # wallet: record it as an ERROR row so it stays visible in the
                # report without aborting the remaining pivots.
                store_findings(
                    db,
                    pivot.address,
                    "Holehe",
                    [("IP Rate Limited", None)],
                    "ERROR",
                )
                db.commit()
                warned += 1
                print(
                    f"  holehe   : WARNING — {blocked} of {report.checked} sites rate-limited; "
                    "logged to the database, absence not verified"
                )
            else:
                print(f"  holehe   : no accounts ({len(report.not_used)} of {report.checked} sites clear)")

    if not pivot.twitter and not pivot.email:
        print("  nothing to run: no handle or email")

    return stored, errors, warned


def seed_test_pivot(db) -> Pivot:
    """Insert Vitalik's known ENS + handle so the pipeline can be exercised."""
    db.execute(
        """
        INSERT INTO wallets (address, ens_name, ens_twitter, last_scanned)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(address) DO UPDATE SET
            ens_name = excluded.ens_name,
            ens_twitter = excluded.ens_twitter
        """,
        (TEST_ADDRESS, TEST_ENS, TEST_TWITTER, utc_now()),
    )
    db.commit()
    return Pivot(address=TEST_ADDRESS, ens_name=TEST_ENS, twitter=TEST_TWITTER, email=None)


def run(limit: int | None = None, timeout: int = DEFAULT_TIMEOUT, test: bool = False) -> int:
    db = init_db()
    try:
        if test:
            pivots = [seed_test_pivot(db)]
            print(f"test pivot: {TEST_ADDRESS} = {TEST_ENS} / @{TEST_TWITTER}\n")
        else:
            pivots = load_pivots(db)
            if not pivots:
                print("no wallets with a Twitter or Email pivot in intel.db")
                print("hint: python intel_engine\\osint_runner.py --test")
                return 0

        if limit is not None:
            pivots = pivots[:limit]

        total = errors = warned = 0
        for index, pivot in enumerate(pivots, start=1):
            print(f"[{index}/{len(pivots)}] ", end="")
            stored, pivot_errors, pivot_warnings = process_pivot(db, pivot, timeout)
            total += stored
            errors += pivot_errors
            warned += pivot_warnings
            print()

        print(
            f"summary: {len(pivots)} pivot(s) · {total} finding(s) stored · "
            f"warnings {warned} · errors {errors}"
        )
        print(f"database: {DB_PATH}")
        return 0 if not errors else 1
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--test",
        action="store_true",
        help=f"inject {TEST_ENS} ({TEST_ADDRESS}) and run the tools on that pivot",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"wall-clock seconds per tool run (default {DEFAULT_TIMEOUT})",
    )
    parser.add_argument("--limit", type=int, help="process at most N pivots")
    args = parser.parse_args(argv)
    return run(limit=args.limit, timeout=args.timeout, test=args.test)


if __name__ == "__main__":
    raise SystemExit(main())
