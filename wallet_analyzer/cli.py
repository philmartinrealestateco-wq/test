"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .analyzer import WalletAnalyzer
from .bep20 import Bep20Provider
from .bulk import (
    ERROR_STATUS,
    append_rows,
    format_target_matches,
    group_by_address,
    run_bulk,
    target_matches,
)
from .config import ConfigError, Settings
from .dashboard import write_dashboard
from .discovery import (
    CHAIN_IDS,
    DiscoveryError,
    discover_holders,
    fetch_token_holders,
    normalize_address,
    write_holders,
)
from .health import exit_code, format_summary, format_table, run_checks
from .inputs import InputError, from_argument, load_file, resolve
from .models import AddressEntry, AnalysisSettings, Network, VerificationStatus
from .net import HttpClient
from .prices import PriceError, PriceService
from .providers import build_providers, describe_modes
from .report import format_usd, write_reports
from .tokens import (
    DEFAULT_BEP20_TOKENS,
    TokenListError,
    load_token_list,
    merge_tokens,
    parse_token_argument,
)
from .validators import validate

LOG = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wallet-analyzer",
        description=(
            "Find wallets whose native-token balance sits inside a USD range and "
            "which show recent on-chain activity. Read-only: public APIs only."
        ),
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="analyse addresses and write reports")
    sources = analyze.add_mutually_exclusive_group()
    sources.add_argument("--input", type=Path, help="CSV/TSV/TXT file of addresses (single batch)")
    sources.add_argument(
        "--file",
        type=Path,
        help=(
            "TXT with one address per line: bulk mode, prints progress and appends "
            "to <out>/report.csv so completed wallets survive a crash"
        ),
    )
    analyze.add_argument(
        "--address",
        action="append",
        default=[],
        metavar="[NETWORK:]ADDRESS",
        help="address to analyse; repeatable, e.g. bitcoin:bc1... or 0x...",
    )
    analyze.add_argument(
        "--network",
        help="force one network for --address values (bitcoin, ethereum, bsc, solana)",
    )
    analyze.add_argument("--min-usd", help="lower balance bound in USD (default 50000)")
    analyze.add_argument("--max-usd", help="upper balance bound in USD (default 900000)")
    analyze.add_argument("--activity-days", type=int, help="activity window in days (default 30)")
    analyze.add_argument("--workers", type=int, help="parallel lookups (default 4)")
    analyze.add_argument("--out", type=Path, help="output directory (default ./out)")
    analyze.add_argument("--no-dashboard", action="store_true", help="skip the HTML dashboard")
    analyze.add_argument("--force-prices", action="store_true", help="bypass the price cache")
    analyze.add_argument("--quiet", action="store_true", help="only print the final summary")
    analyze.add_argument(
        "--filter",
        action="store_true",
        help=(
            "with --file: also write <out>/report_matches.csv containing only the "
            "wallets whose Total USD is inside the balance range"
        ),
    )
    analyze.add_argument(
        "--no-tokens",
        action="store_true",
        help="skip BEP-20 balances (native coin only)",
    )
    analyze.add_argument(
        "--token",
        action="append",
        default=[],
        metavar="0xCONTRACT[:SYMBOL[:DECIMALS]]",
        help="extra BEP-20 contract to check; repeatable",
    )
    analyze.add_argument(
        "--token-file",
        type=Path,
        help="CSV of BEP-20 contracts (contract[,symbol[,decimals]])",
    )
    analyze.set_defaults(handler=cmd_analyze)

    prices = subparsers.add_parser("prices", help="show the current USD price snapshot")
    prices.add_argument("--force", action="store_true", help="bypass the price cache")
    prices.set_defaults(handler=cmd_prices)

    providers = subparsers.add_parser("providers", help="show which source serves each network")
    providers.set_defaults(handler=cmd_providers)

    doctor = subparsers.add_parser(
        "doctor", help="test every external service and report what needs configuration"
    )
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.set_defaults(handler=cmd_doctor)

    discover = subparsers.add_parser(
        "discover", help="build a --file wallet list from a token's holders"
    )
    discover.add_argument("--token", required=True, help="token contract address (0x + 40 hex)")
    discover.add_argument("--chain", default="bsc", help="bsc (default) or ethereum")
    discover.add_argument(
        "--csv",
        type=Path,
        help="local CSV export used when the API is unavailable (first column = wallet)",
    )
    discover.add_argument(
        "--out",
        type=Path,
        help="output .txt path (default <out>/holders_<contract>.txt)",
    )
    discover.add_argument(
        "--limit", type=int, default=100, help="how many holders to request (default 100)"
    )
    discover.set_defaults(handler=cmd_discover)

    validate_cmd = subparsers.add_parser("validate", help="validate one address without network calls")
    validate_cmd.add_argument("--network", required=True)
    validate_cmd.add_argument("--address", required=True)
    validate_cmd.set_defaults(handler=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    level_name = (args.log_level or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(levelname)-7s %(name)s | %(message)s",
        stream=sys.stderr,
    )

    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except (ConfigError, InputError, TokenListError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except PriceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("refusing to value wallets without a price feed", file=sys.stderr)
        return 3


def cmd_analyze(args: argparse.Namespace) -> int:
    if args.filter and not args.file:
        print("error: --filter requires --file (bulk mode)", file=sys.stderr)
        return 2

    settings = Settings.load().with_overrides(
        min_usd=_decimal_arg(args.min_usd, "--min-usd"),
        max_usd=_decimal_arg(args.max_usd, "--max-usd"),
        activity_days=args.activity_days,
        max_workers=_worker_arg(args.workers),
        output_dir=args.out,
    )

    entries = _collect_entries(args)
    if not entries:
        print("error: no addresses supplied; use --file, --input or --address", file=sys.stderr)
        return 2

    analysis_settings = AnalysisSettings(
        min_usd=settings.min_usd,
        max_usd=settings.max_usd,
        activity_days=settings.activity_days,
    )

    with HttpClient(timeout=settings.request_timeout) as client:
        providers = build_providers(settings, client)
        modes = describe_modes(providers)
        price_service = PriceService(
            client,
            ttl_seconds=settings.price_ttl_seconds,
            cache_path=settings.cache_dir / "prices.json",
            token_cache_path=settings.cache_dir / "token_prices.json",
        )
        bep20 = _build_bep20(settings, args, client)
        analyzer = WalletAnalyzer(
            providers,
            price_service,
            analysis_settings,
            max_workers=settings.max_workers,
            bep20=bep20,
        )

        snapshot = price_service.get(force_refresh=args.force_prices)

        if args.file:
            report_path = settings.output_dir / "report.csv"
            matches_path = (
                settings.output_dir / "report_matches.csv" if args.filter else None
            )
            groups = group_by_address(entries)
            print(f"bulk mode: {len(groups)} wallet(s) from {args.file.name}")
            print(f"report: {report_path} (appending)")
            if matches_path is not None:
                print(f"matches: {matches_path} (appending)")
            print()
            rows = run_bulk(
                groups,
                analyzer,
                snapshot,
                report_path,
                progress=None if args.quiet else _bulk_progress,
            )
            matches = target_matches(rows, settings.min_usd, settings.max_usd)
            if matches_path is not None:
                append_rows(matches_path, matches)
            _print_bulk_summary(rows, report_path, matches_path)
            print()
            print(
                format_target_matches(
                    matches,
                    settings.min_usd,
                    settings.max_usd,
                    matches_path=matches_path,
                )
            )
            return 0

        if not args.quiet:
            _print_plan(entries, modes, analysis_settings, bep20)

        result = analyzer.analyze(
            entries,
            prices=snapshot,
            progress=None if args.quiet else _progress_printer(),
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths = write_reports(result, settings.output_dir, timestamp=result.finished_at)
    dashboard_path = None
    if not args.no_dashboard:
        dashboard_path = write_dashboard(
            result, settings.output_dir / f"dashboard_{stamp}.html", provider_modes=modes
        )

    _print_summary(result, paths, dashboard_path)
    return 0


def cmd_prices(args: argparse.Namespace) -> int:
    settings = Settings.load()
    with HttpClient(timeout=settings.request_timeout) as client:
        snapshot = PriceService(
            client,
            ttl_seconds=settings.price_ttl_seconds,
            cache_path=settings.cache_dir / "prices.json",
        ).get(force_refresh=args.force)

    print(f"source: {snapshot.source}")
    print(f"as of : {snapshot.fetched_at.astimezone(timezone.utc).isoformat(timespec='seconds')}")
    if snapshot.stale:
        print("note  : cached values, the upstream feed was unreachable")
    for asset, value in snapshot.prices.items():
        print(f"  {asset:<4} ${value:,.2f}")
    return 0


def cmd_providers(_args: argparse.Namespace) -> int:
    settings = Settings.load()
    with HttpClient(timeout=settings.request_timeout) as client:
        modes = describe_modes(build_providers(settings, client))

    summary = settings.public_summary()
    print(
        "keys: ETHERSCAN_API_KEY={e}  BSCSCAN_API_KEY={b}  "
        "BLOCKCHAIN_EXPLORER_API_KEY={c}".format(
            e=summary["etherscan_key"], b=summary["bscscan_key"], c=summary["blockchain_key"]
        )
    )
    for network in Network:
        print(f"  {network.label:<16} {modes.get(network, 'unavailable')}")
    if not settings.has_bsc_activity_key:
        print(
            "\nBNB Chain activity cannot be verified without an Etherscan V2 key that "
            "covers chainid=56, so BSC wallets are reported as PARTIAL."
        )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = Settings.load()
    with HttpClient(timeout=settings.request_timeout) as client:
        checks = run_checks(settings, client)

    summary = settings.public_summary()

    if args.json:
        print(
            json.dumps(
                {
                    "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "credentials": {
                        "ETHERSCAN_API_KEY": summary["etherscan_key"],
                        "BSCSCAN_API_KEY": summary["bscscan_key"],
                        "BLOCKCHAIN_EXPLORER_API_KEY": summary["blockchain_key"],
                        "bsc_activity_key": summary["bsc_activity"],
                    },
                    "endpoints": {
                        "solana_rpc": summary["solana_rpc"],
                        "bsc_rpc": summary["bsc_rpc"],
                    },
                    "checks": [check.as_dict() for check in checks],
                },
                indent=2,
            )
        )
    else:
        print(format_table(checks))
        print()
        print(format_summary(checks))
        print(
            "keys: ETHERSCAN_API_KEY={e}  BSCSCAN_API_KEY={b}  "
            "BLOCKCHAIN_EXPLORER_API_KEY={c}".format(
                e=summary["etherscan_key"],
                b=summary["bscscan_key"],
                c=summary["blockchain_key"],
            )
        )
        print(f"endpoints: solana {summary['solana_rpc']} | bsc {summary['bsc_rpc']}")
        print("all checks are read-only: nothing is signed or broadcast")

    return exit_code(checks)


def cmd_discover(args: argparse.Namespace) -> int:
    contract = normalize_address(args.token)
    if contract is None:
        print(
            f"error: --token must be a 0x-prefixed 20-byte contract address, got {args.token!r}",
            file=sys.stderr,
        )
        return 2

    chain_id = CHAIN_IDS.get(args.chain.strip().lower())
    if chain_id is None:
        print(
            f"error: unknown chain {args.chain!r}; expected bsc or ethereum",
            file=sys.stderr,
        )
        return 2

    if not 1 <= args.limit <= 1000:
        print("error: --limit must be between 1 and 1000", file=sys.stderr)
        return 2

    settings = Settings.load()
    out_path = args.out or settings.output_dir / f"holders_{contract}.txt"

    def api_fetch() -> list[str]:
        if not settings.has_etherscan_key:
            raise ConfigError("ETHERSCAN_API_KEY is not set")
        with HttpClient(timeout=settings.request_timeout) as client:
            return fetch_token_holders(
                client,
                settings.etherscan_api_key or "",
                contract,
                chain_id=chain_id,
                offset=args.limit,
            )

    try:
        addresses, source = discover_holders(
            api_fetch, csv_path=args.csv, limit=args.limit
        )
    except DiscoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    count = write_holders(out_path, addresses)
    print(f"contract: {contract}")
    print(f"source  : {source}")
    print(f"holders : {count}")
    print(f"written : {out_path}")
    print(f"next    : python -m wallet_analyzer analyze --file {out_path}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        network = Network.parse(args.network)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    outcome = validate(network, args.address)
    if outcome.valid:
        print(f"valid {network.label} address")
        if outcome.normalized != args.address.strip():
            print(f"normalized: {outcome.normalized}")
        return 0

    print(f"invalid {network.label} address: {outcome.reason}", file=sys.stderr)
    return 1


# -- helpers -------------------------------------------------------------


def _collect_entries(args: argparse.Namespace) -> list[AddressEntry]:
    entries: list[AddressEntry] = []

    source = getattr(args, "file", None) or args.input
    if source:
        parsed = load_file(source)
        for warning in parsed.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        entries.extend(parsed.entries)

    if args.address:
        forced: Network | None = None
        if args.network:
            try:
                forced = Network.parse(args.network)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        raw = [from_argument(value, forced) for value in args.address]
        parsed = resolve(raw)
        for warning in parsed.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        entries.extend(parsed.entries)

    return entries


def _build_bep20(settings: Settings, args: argparse.Namespace, client: HttpClient) -> Bep20Provider | None:
    if args.no_tokens or not settings.bep20_enabled:
        return None

    token_file = args.token_file or settings.token_list_file
    groups: list[tuple] = [DEFAULT_BEP20_TOKENS]
    if token_file:
        groups.append(tuple(load_token_list(token_file)))
    if args.token:
        groups.append(tuple(parse_token_argument(value) for value in args.token))

    return Bep20Provider(client, settings.bsc_rpc_url, merge_tokens(*groups))


def _print_plan(
    entries: list[AddressEntry],
    modes: dict[Network, str],
    settings: AnalysisSettings,
    bep20: Bep20Provider | None = None,
) -> None:
    counts: dict[Network, int] = {}
    for entry in entries:
        counts[entry.network] = counts.get(entry.network, 0) + 1

    print(f"analysing {len(entries)} address/network pair(s)")
    print(f"criteria: {settings.describe()}")
    for network, count in counts.items():
        print(f"  {network.label:<16} {count:>4}  via {modes.get(network, 'unavailable')}")
    if bep20 is not None and counts.get(Network.BSC):
        print(
            f"  BEP-20 allowlist: {len(bep20.contracts)} contract(s) via {bep20.endpoint} "
            "(unlisted tokens are not detected)"
        )
    print()


def _bulk_progress(index: int, total: int, address: str) -> None:
    print(f"[{index}/{total}] Processing {_short(address)}", flush=True)


def _print_bulk_summary(
    rows, report_path: Path, matches_path: Path | None = None
) -> None:
    failures = sum(1 for row in rows if row.status == ERROR_STATUS)
    wallets = len({row.wallet for row in rows})
    print()
    print(f"bulk complete: {len(rows)} row(s) across {wallets} wallet(s)")
    if failures:
        print(f"  failures: {failures} (ERROR rows kept in the report)")
    print(f"  report  : {report_path}")
    if matches_path is not None:
        print(f"  matches : {matches_path}")


def _progress_printer():
    def report(record, done: int, total: int) -> None:
        usd = format_usd(record.usd_value) or "n/a"
        flag = "!" if record.verification_status is not VerificationStatus.VERIFIED else " "
        print(
            f"{flag} [{done:>3}/{total}] {record.network.native_symbol:<4} "
            f"{_short(record.address):<22} {usd:>15}  "
            f"{record.activity_status.value:<8} {record.verification_status.value}"
        )

    return report


def _print_summary(result, paths, dashboard_path: Path | None) -> None:
    print()
    print("summary")
    print(f"  addresses analysed : {result.total_addresses}")
    print(f"  fully verified     : {result.verified_count}")
    print(f"  active wallets     : {result.active_count}")
    print(f"  inside range       : {result.in_range_count}")
    print(f"  matching wallets   : {len(result.matches)}")
    print(f"  matched value      : ${result.matched_total_usd:,.2f}")
    if any(record.tokens_checked for record in result.records):
        print(f"  BEP-20 holders     : {result.wallets_with_tokens}")
        print(f"  BEP-20 value       : ${result.token_value_usd:,.2f}")
    print(f"  prices             : {result.price_source}")
    print()
    print("reports")
    print(f"  matches CSV        : {paths.matches_csv}")
    print(f"  full CSV           : {paths.all_csv}")
    print(f"  Excel workbook     : {paths.workbook}")
    if dashboard_path:
        print(f"  dashboard          : {dashboard_path}")


def _short(address: str) -> str:
    return address if len(address) <= 22 else f"{address[:10]}..{address[-8:]}"


def _decimal_arg(value: str | None, label: str) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value).replace(",", "").replace("$", ""))
    except InvalidOperation as exc:
        raise ConfigError(f"{label} expects a number, got {value!r}") from exc
    if parsed < 0:
        raise ConfigError(f"{label} must not be negative")
    return parsed


def _worker_arg(value: int | None) -> int | None:
    if value is None:
        return None
    if not 1 <= value <= 32:
        raise ConfigError("--workers must be between 1 and 32")
    return value
