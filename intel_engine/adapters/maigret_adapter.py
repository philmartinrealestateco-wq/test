"""Maigret adapter: username search over Maigret's site database.

Maigret expands one handle into several username variants (``VitalikButerin``,
``vitalik.buterin``, ``vitalik``) and writes **one ndjson report per variant**
inside the output folder. All of them are merged here, deduplicated by canonical
profile URL, and each account keeps the variant that produced it.

Every failure path is explicit: a run that writes no report, or that reports
more accounts than were parsed, is an error - never a silent "no accounts".
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from adapters import canonical_url

SCRIPTS = Path(__file__).resolve().parent.parent / ".venv" / "Scripts"
MAIGRET = SCRIPTS / "maigret.exe"
DEFAULT_TOP_SITES = 100
DEFAULT_TIMEOUT = 300
PER_REQUEST_TIMEOUT = "10"

_SCOPE = re.compile(r"Starting a search on top (?P<count>\d+) sites")
_REPORTED = re.compile(r"returned (?P<count>\d+) accounts")
_REPORT_NAME = re.compile(r"report_(?P<variant>.+)_ndjson\.json$")


@dataclass(frozen=True)
class Account:
    site: str
    url: str
    variant: str = ""


@dataclass
class MaigretResult:
    accounts: list[Account] = field(default_factory=list)
    scope: int | None = None
    parsed: int = 0
    reported: int = 0
    error: str | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


def find_reports(directory: Path) -> list[Path]:
    """Every ndjson report Maigret wrote, one per username variant."""
    return sorted(directory.glob("report_*_ndjson.json"))


def variant_of(path: Path) -> str:
    match = _REPORT_NAME.match(path.name)
    return match["variant"] if match else ""


def parse_report(path: Path) -> list[Account]:
    """Return the claimed profiles in one report, deduplicated by URL."""
    variant = variant_of(path)
    accounts: list[Account] = []
    seen: set[str] = set()

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            status = row.get("status")
            if isinstance(status, dict):
                claimed = str(status.get("status", "")).lower() == "claimed"
                url = status.get("url") or row.get("url_user")
                site = status.get("site_name") or row.get("sitename")
            else:
                claimed = str(status or "").lower() == "claimed"
                url = row.get("url_user")
                site = row.get("sitename")

            if not claimed or not url or not site:
                continue

            key = canonical_url(url)
            if key in seen:
                continue
            seen.add(key)
            accounts.append(Account(site=str(site), url=str(url), variant=variant))

    return accounts


def parse_reports(paths: list[Path]) -> tuple[list[Account], int]:
    """Merge every report, returning the deduplicated accounts and the raw count."""
    merged: list[Account] = []
    seen: set[str] = set()
    raw = 0

    for path in paths:
        accounts = parse_report(path)
        raw += len(accounts)
        for account in accounts:
            key = canonical_url(account.url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(account)

    return merged, raw


def run(
    handle: str,
    timeout: int = DEFAULT_TIMEOUT,
    top_sites: int = DEFAULT_TOP_SITES,
    per_request_timeout: str = PER_REQUEST_TIMEOUT,
    executable: Path = MAIGRET,
) -> MaigretResult:
    """Scan ``handle`` and merge the profiles from every variant report."""
    if not Path(executable).is_file():
        return MaigretResult(error=f"maigret not found at {executable}")

    command = [
        str(executable),
        handle,
        "--top-sites",
        str(top_sites),
        "--timeout",
        per_request_timeout,
        "--no-progressbar",
        "--no-color",
        "-J",
        "ndjson",
        "--folderoutput",
        ".",
    ]

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
                check=False,
            )
        except subprocess.TimeoutExpired:
            return MaigretResult(error=f"timed out after {timeout}s", timed_out=True)
        except OSError as error:
            return MaigretResult(error=f"could not start: {error}")

        stdout = completed.stdout or ""
        scope_match = _SCOPE.search(stdout)
        scope = int(scope_match["count"]) if scope_match else None
        reported = sum(int(match["count"]) for match in _REPORTED.finditer(stdout))

        reports = find_reports(Path(workdir))
        if not reports:
            return MaigretResult(
                error=f"no ndjson report written (exit {completed.returncode})",
                scope=scope,
            )

        try:
            accounts, raw = parse_reports(reports)
        except OSError as error:
            return MaigretResult(error=f"could not read the report: {error}", scope=scope)

    result = MaigretResult(accounts=accounts, scope=scope, parsed=raw, reported=reported)

    if reported and raw < reported:
        result.error = f"parsed {raw} of {reported} account(s) Maigret reported"
    elif completed.returncode not in (0, None) and not accounts:
        result.error = f"exit {completed.returncode} with nothing to parse"

    return result
