"""Phase 2b: screen the cached target wallets against OpenSanctions.

Downloads the OpenSanctions ``us_ofac_sdn`` bulk export and matches every
``CryptoWallet`` address in it against the wallets already cached in
``intel.db``. A match becomes a ``LIST HIT`` row in ``osint_findings``; a run
that finds nothing still records a ``sanctions_runs`` row, so "checked, no
match" never looks like "never checked".

The data is OpenSanctions, licensed CC BY-NC 4.0:
https://www.opensanctions.org/licensing/ - non-commercial use only.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

from database import DB_PATH, init_db
from onchain_scraper import MASTER_CSV, read_targets, utc_now

DEFAULT_DATASET = "us_ofac_sdn"
INDEX_URL = "https://data.opensanctions.org/datasets/latest/{dataset}/index.json"
ENTITY_URL = "https://www.opensanctions.org/entities/{entity_id}/"
CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "opensanctions"
EXPORT_NAME = "entities.ftm.json"
TOOL = "OpenSanctions"
CONFIDENCE = "LIST HIT"
USER_AGENT = "intel_engine-sanctions/1.0"
TIMEOUT = 120


class SanctionsError(RuntimeError):
    """A dataset could not be fetched or parsed; never reported as zero hits."""


@dataclass(frozen=True)
class DatasetInfo:
    name: str
    updated: str | None
    source_url: str
    size: int
    path: Path
    entities: int
    wallets: int
    records: list["SanctionedWallet"]


@dataclass(frozen=True)
class SanctionedWallet:
    address: str
    currency: str | None
    holder: str | None
    entity_id: str


def address_key(value: str) -> str:
    """Compare case-insensitively only where the chain is case-insensitive.

    EVM addresses are hex and casing is irrelevant; Base58 addresses (Tron,
    Bitcoin) are case-sensitive, so lowering them would invent matches.
    """
    text = value.strip()
    if text[:2].lower() == "0x" and len(text) == 42:
        return text.lower()
    return text


def fetch_index(dataset: str) -> dict:
    url = INDEX_URL.format(dataset=dataset)
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        raise SanctionsError(f"could not read the {dataset} index from {url}: {error}") from error


def find_export(index: dict) -> dict:
    for resource in index.get("resources", []):
        if resource.get("name") == EXPORT_NAME:
            return resource
    raise SanctionsError(f"{index.get('name')} has no {EXPORT_NAME} resource")


def download(dataset: str, index: dict, refresh: bool = False) -> Path:
    """Return the cached export, downloading it when missing or stale."""
    export = find_export(index)
    path = CACHE_DIR / dataset / EXPORT_NAME

    if not refresh and path.is_file() and path.stat().st_size == export.get("size"):
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    try:
        with requests.get(
            export["url"],
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
            stream=True,
        ) as response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    handle.write(chunk)
    except (requests.RequestException, OSError) as error:
        partial.unlink(missing_ok=True)
        raise SanctionsError(f"could not download {export['url']}: {error}") from error

    if partial.stat().st_size != export.get("size"):
        partial.unlink(missing_ok=True)
        raise SanctionsError(
            f"downloaded {partial.stat().st_size:,} bytes but the index promised "
            f"{export.get('size'):,} - refusing to screen against a truncated list"
        )

    partial.replace(path)
    return path


def read_wallets(path: Path) -> tuple[list[SanctionedWallet], int]:
    """Collect every ``CryptoWallet`` in the export and resolve its holder name."""
    wallets: list[SanctionedWallet] = []
    captions: dict[str, str] = {}
    entities = 0

    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entity = json.loads(line)
                except json.JSONDecodeError as error:
                    raise SanctionsError(f"{path.name} is not valid FollowTheMoney JSON: {error}") from error
                entities += 1
                entity_id = entity.get("id")
                if entity.get("schema") == "CryptoWallet":
                    properties = entity.get("properties") or {}
                    for value in properties.get("publicKey") or []:
                        wallets.append(
                            SanctionedWallet(
                                address=value,
                                currency=(properties.get("currency") or [None])[0],
                                holder=(properties.get("holder") or [None])[0],
                                entity_id=entity_id,
                            )
                        )
                elif entity_id:
                    captions[entity_id] = entity.get("caption") or entity_id
    except OSError as error:
        raise SanctionsError(f"could not read {path}: {error}") from error

    if not wallets:
        raise SanctionsError(f"{path} contained no CryptoWallet entities")

    resolved = [
        SanctionedWallet(
            address=wallet.address,
            currency=wallet.currency,
            holder=captions.get(wallet.holder) if wallet.holder else None,
            entity_id=wallet.entity_id,
        )
        for wallet in wallets
    ]
    return resolved, entities


def load_dataset(dataset: str = DEFAULT_DATASET, refresh: bool = False) -> DatasetInfo:
    index = fetch_index(dataset)
    export = find_export(index)
    path = download(dataset, index, refresh=refresh)
    wallets, entities = read_wallets(path)
    return DatasetInfo(
        name=dataset,
        updated=index.get("last_change"),
        source_url=export["url"],
        size=export.get("size") or path.stat().st_size,
        path=path,
        entities=entities,
        wallets=len(wallets),
        records=wallets,
    )


def load_targets(db) -> list[str]:
    rows = db.execute("SELECT address FROM wallets ORDER BY lower(address)").fetchall()
    return [row["address"] for row in rows]


def uncached_targets(db) -> int:
    """Count master-CSV targets that phase 2 has not cached yet."""
    try:
        targets = read_targets(MASTER_CSV, limit=None)
    except OSError:
        return 0
    cached = {address_key(row) for row in load_targets(db)}
    return sum(1 for target in targets if address_key(target.address) not in cached)


def match_targets(targets: list[str], dataset: DatasetInfo) -> list[tuple[str, SanctionedWallet]]:
    sanctioned: dict[str, list[SanctionedWallet]] = {}
    for wallet in dataset.records:
        sanctioned.setdefault(address_key(wallet.address), []).append(wallet)

    matches: list[tuple[str, SanctionedWallet]] = []
    for address in targets:
        for wallet in sanctioned.get(address_key(address), []):
            matches.append((address, wallet))
    return matches


def finding_text(wallet: SanctionedWallet, dataset: str) -> str:
    holder = wallet.holder or "unnamed sanctioned entity"
    currency = f" ({wallet.currency})" if wallet.currency else ""
    return f"{dataset} listing: {holder}{currency}"


def store_matches(db, matches: list[tuple[str, SanctionedWallet]], dataset: str) -> int:
    """Append one LIST HIT row per match, skipping rows already stored."""
    now = utc_now()
    stored = 0
    for address, wallet in matches:
        finding = finding_text(wallet, dataset)
        url = ENTITY_URL.format(entity_id=wallet.entity_id)
        cursor = db.execute(
            """
            INSERT INTO osint_findings (address, tool, finding, url, confidence, timestamp)
            SELECT ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM osint_findings
                WHERE lower(address) = lower(?)
                  AND tool = ?
                  AND finding = ?
                  AND ifnull(url, '') = ?
            )
            """,
            (address, TOOL, finding, url, CONFIDENCE, now, address, TOOL, finding, url),
        )
        stored += cursor.rowcount
    db.commit()
    return stored


def record_run(
    db,
    dataset: DatasetInfo,
    checked: int,
    matches: int,
) -> None:
    db.execute(
        """
        INSERT INTO sanctions_runs
            (dataset, source_url, dataset_updated, entities, wallets, checked, matches, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dataset.name,
            dataset.source_url,
            dataset.updated,
            dataset.entities,
            dataset.wallets,
            checked,
            matches,
            utc_now(),
        ),
    )
    db.commit()


def run(
    datasets: tuple[str, ...] = (DEFAULT_DATASET,),
    refresh: bool = False,
    dry_run: bool = False,
) -> int:
    db = init_db()
    try:
        targets = load_targets(db)
        if not targets:
            print("no target wallets cached in intel.db")
            print("hint: run run_intel.py first, or seed the wallets table")
            return 1

        missing = uncached_targets(db)
        if missing:
            print(
                f"note: {missing} master_targets.csv address(es) are not cached in intel.db; "
                "only cached wallets can be screened and reported"
            )

        total_matches = 0
        for name in datasets:
            print(f"== {name} ==")
            dataset = load_dataset(name, refresh=refresh)
            print(
                f"  export   : {dataset.entities:,} entities, {dataset.wallets:,} sanctioned wallets"
                f" ({dataset.size / 1_048_576:.1f} MB, updated {dataset.updated or 'unknown'})"
            )
            matches = match_targets(targets, dataset)

            if matches:
                stored = 0 if dry_run else store_matches(db, matches, name)
                for address, wallet in matches:
                    print(f"  MATCH    : {address} -> {finding_text(wallet, name)}")
                print(f"  matched  : {len(matches)} target(s), {stored} new row(s) stored")
            else:
                print(f"  matched  : 0 of {len(targets)} target(s) against {dataset.wallets:,} wallets")

            if not dry_run:
                record_run(db, dataset, checked=len(targets), matches=len(matches))
            total_matches += len(matches)
            print()

        print(f"summary: {len(targets)} wallet(s) screened · {total_matches} match(es)")
        if dry_run:
            print("dry run: nothing written to the database")
        print(f"database: {DB_PATH}")
        print(f"cache   : {CACHE_DIR}")
        return 0
    except SanctionsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dataset",
        action="append",
        dest="datasets",
        metavar="NAME",
        help=f"OpenSanctions dataset to screen (repeatable, default {DEFAULT_DATASET})",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-download the export even when the cached copy matches the index size",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report matches without writing findings or a run record",
    )
    args = parser.parse_args(argv)
    return run(
        datasets=tuple(args.datasets or (DEFAULT_DATASET,)),
        refresh=args.refresh,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
