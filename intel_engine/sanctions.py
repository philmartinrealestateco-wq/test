"""Phase 2b: screen the cached target wallets against OpenSanctions.

Two modes share one dataset cache:

* addresses - every ``CryptoWallet`` address in the OFAC SDN export is compared
  with the wallets already cached in ``intel.db``; a hit is stored as a
  ``LIST HIT`` row.
* names - an entity name (from an ENS record, a web mention or the command
  line) is matched against the captions, aliases and weak aliases of the same
  export; an exact name or alias is stored as ``VERIFIED``, a token-level
  containment as ``POSSIBLE``.

Both modes are local: the export is downloaded once (size-checked against the
index, cached under ``.cache/``) and every match is computed in-process, so no
request leaves the machine and there is no remote quota to throttle. Repeat
screening of a name is skipped through ``sanctions_names`` until the dataset
version changes, which is what keeps a rerun cheap.

The data is OpenSanctions, licensed CC BY-NC 4.0:
https://www.opensanctions.org/licensing/ - non-commercial use only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
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
CONFIDENCE_VERIFIED = "VERIFIED"
CONFIDENCE_POSSIBLE = "POSSIBLE"
CATEGORY = "sanctions"
MAX_NAME_MATCHES = 20
USER_AGENT = "intel_engine-sanctions/1.0"
TIMEOUT = 120

# Legal-form tokens are stripped before comparison so "ACME BANK, LTD." and
# "Acme Bank" are the same key.
LEGAL_SUFFIXES = frozenset(
    {
        "ltd", "limited", "llc", "inc", "incorporated", "plc", "corp",
        "corporation", "co", "company", "gmbh", "ag", "sa", "bv", "nv",
        "oy", "ab", "srl", "spa", "pte", "kk", "jsc", "ojsc", "pjsc",
    }
)


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


@dataclass(frozen=True)
class NameEntry:
    entity_id: str
    caption: str
    schema: str | None
    source: str
    confidence: str


@dataclass(frozen=True)
class NameMatch:
    entity_id: str
    caption: str
    schema: str | None
    matched_key: str
    source: str
    confidence: str


@dataclass
class NameIndex:
    names: dict[str, list[NameEntry]] = field(default_factory=dict)
    entities: int = 0


@dataclass(frozen=True)
class NameQuery:
    address: str | None
    query: str


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
            INSERT INTO osint_findings
                (address, tool, finding, url, confidence, timestamp, category)
            SELECT ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM osint_findings
                WHERE lower(address) = lower(?)
                  AND tool = ?
                  AND finding = ?
                  AND ifnull(url, '') = ?
            )
            """,
            (address, TOOL, finding, url, CONFIDENCE, now, CATEGORY, address, TOOL, finding, url),
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


def strip_ens_suffix(value: str) -> str:
    """Drop a trailing ``.eth`` so the ENS namespace does not skew the match."""
    text = value.strip()
    if text.lower().endswith(".eth"):
        return text[: -len(".eth")]
    return text


def normalise_name(value: str) -> str:
    """Fold case, accents, punctuation and legal suffixes into a comparison key."""
    text = unicodedata.normalize("NFKD", value)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    tokens = text.split()
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def read_name_index(path: Path) -> NameIndex:
    """Index every caption, name, alias and weak alias in the export."""
    index = NameIndex()
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entity = json.loads(line)
                except json.JSONDecodeError as error:
                    raise SanctionsError(
                        f"{path.name} is not valid FollowTheMoney JSON: {error}"
                    ) from error

                index.entities += 1
                entity_id = entity.get("id")
                caption = entity.get("caption") or ""
                properties = entity.get("properties") or {}
                candidates: list[tuple[str, str, str]] = []
                if caption:
                    candidates.append((caption, "caption", CONFIDENCE_VERIFIED))
                for source, confidence in (
                    ("name", CONFIDENCE_VERIFIED),
                    ("alias", CONFIDENCE_VERIFIED),
                    ("weakAlias", CONFIDENCE_POSSIBLE),
                ):
                    for value in properties.get(source) or []:
                        candidates.append((value, source, confidence))

                for value, source, confidence in candidates:
                    key = normalise_name(value)
                    if len(key) < 3:
                        continue
                    index.names.setdefault(key, []).append(
                        NameEntry(
                            entity_id=entity_id,
                            caption=caption or value,
                            schema=entity.get("schema"),
                            source=source,
                            confidence=confidence,
                        )
                    )
    except OSError as error:
        raise SanctionsError(f"could not read {path}: {error}") from error

    if not index.names:
        raise SanctionsError(f"{path} contained no usable entity names")
    return index


def screen_name(query: str, index: NameIndex) -> list[NameMatch]:
    """Return scored name matches: exact caption/alias first, then containment.

    Partial matching needs at least two tokens, so a single generic word cannot
    drag in hundreds of unrelated entities.
    """
    key = normalise_name(strip_ens_suffix(query))
    if not key:
        return []

    matches: dict[str, NameMatch] = {}

    def remember(entry: NameEntry, matched_key: str) -> None:
        existing = matches.get(entry.entity_id)
        if existing is not None and existing.confidence == CONFIDENCE_VERIFIED:
            return
        matches[entry.entity_id] = NameMatch(
            entity_id=entry.entity_id,
            caption=entry.caption,
            schema=entry.schema,
            matched_key=matched_key,
            source=entry.source,
            confidence=entry.confidence,
        )

    for entry in index.names.get(key, []):
        remember(entry, key)

    tokens = set(key.split())
    if len(tokens) >= 2:
        for candidate, entries in index.names.items():
            if candidate == key:
                continue
            candidate_tokens = set(candidate.split())
            if len(candidate_tokens) < len(tokens) or len(candidate_tokens) > len(tokens) + 4:
                continue
            if not tokens <= candidate_tokens:
                continue
            for entry in entries:
                if entry.entity_id in matches:
                    continue
                matches[entry.entity_id] = NameMatch(
                    entity_id=entry.entity_id,
                    caption=entry.caption,
                    schema=entry.schema,
                    matched_key=candidate,
                    source=entry.source,
                    confidence=CONFIDENCE_POSSIBLE,
                )
            if len(matches) >= MAX_NAME_MATCHES * 4:
                break

    ordered = sorted(
        matches.values(),
        key=lambda match: (0 if match.confidence == CONFIDENCE_VERIFIED else 1, match.caption.lower()),
    )
    return ordered[:MAX_NAME_MATCHES]


def name_finding(match: NameMatch) -> str:
    schema = f" ({match.schema})" if match.schema else ""
    return f"Name match: {match.caption}{schema}"


def store_name_matches(db, address: str, matches: list[NameMatch]) -> int:
    """Append one row per name match, skipping rows already stored."""
    now = utc_now()
    stored = 0
    for match in matches:
        finding = name_finding(match)
        url = ENTITY_URL.format(entity_id=match.entity_id)
        cursor = db.execute(
            """
            INSERT INTO osint_findings
                (address, tool, finding, url, confidence, timestamp, category)
            SELECT ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM osint_findings
                WHERE lower(address) = lower(?)
                  AND tool = ?
                  AND finding = ?
                  AND ifnull(url, '') = ?
            )
            """,
            (
                address, TOOL, finding, url, match.confidence, now, CATEGORY,
                address, TOOL, finding, url,
            ),
        )
        stored += cursor.rowcount
    db.commit()
    return stored


def lookup_name_screening(db, name_key: str, dataset: str, dataset_updated: str | None):
    """The cached screening for this key, valid only for the same dataset build."""
    return db.execute(
        """
        SELECT query, matches, best_caption, best_confidence, checked_at
        FROM sanctions_names
        WHERE name_key = ? AND dataset = ? AND ifnull(dataset_updated, '') = ifnull(?, '')
        """,
        (name_key, dataset, dataset_updated),
    ).fetchone()


def record_name_screening(
    db,
    name_key: str,
    query: str,
    dataset: DatasetInfo,
    matches: list[NameMatch],
) -> None:
    """Upsert the screening outcome so a rerun can skip the comparison."""
    best = matches[0] if matches else None
    db.execute(
        """
        INSERT OR REPLACE INTO sanctions_names
            (name_key, query, dataset, dataset_updated, matches,
             best_entity_id, best_caption, best_confidence, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name_key,
            query,
            dataset.name,
            dataset.updated,
            len(matches),
            best.entity_id if best else None,
            best.caption if best else None,
            best.confidence if best else None,
            utc_now(),
        ),
    )
    db.commit()


def load_ens_names(db) -> list[tuple[str, str]]:
    return [
        (row["address"], row["ens_name"])
        for row in db.execute(
            """
            SELECT address, ens_name FROM wallets
            WHERE ens_name IS NOT NULL AND TRIM(ens_name) <> ''
            ORDER BY lower(address)
            """
        )
    ]


def gather_name_queries(
    db,
    names: tuple[str, ...],
    from_ens: bool,
    address: str | None,
) -> list[NameQuery]:
    queries: list[NameQuery] = []
    for value in names:
        queries.append(NameQuery(address=address if len(names) == 1 else None, query=value))
    if from_ens:
        queries.extend(NameQuery(address=wallet, query=ens) for wallet, ens in load_ens_names(db))
    return queries


def screen_names(
    db,
    dataset: DatasetInfo,
    queries: list[NameQuery],
    refresh: bool,
    dry_run: bool,
) -> int:
    """Screen each name against the dataset, using the cache when it is current."""
    index = read_name_index(dataset.path)
    print(
        f"  export   : {index.entities:,} entities, {len(index.names):,} name key(s)"
        f" (updated {dataset.updated or 'unknown'})"
    )

    total = 0
    for item in queries:
        key = normalise_name(strip_ens_suffix(item.query))
        if not key:
            print(f"  skipped  : {item.query!r} leaves no comparable name")
            continue

        cached = None if refresh else lookup_name_screening(db, key, dataset.name, dataset.updated)
        if cached is not None:
            total += cached["matches"] or 0
            print(
                f"  cached   : {item.query} -> {cached['matches']} match(es)"
                f" recorded {cached['checked_at']}"
            )
            continue

        matches = screen_name(item.query, index)
        total += len(matches)

        if not dry_run:
            if item.address and matches:
                store_name_matches(db, item.address, matches)
            record_name_screening(db, key, item.query, dataset, matches)

        if matches:
            target = f" for {item.address}" if item.address else ""
            print(f"  MATCH    : {item.query!r}{target} -> {len(matches)} candidate(s)")
            for match in matches:
                print(f"             * {match.confidence}: {match.caption} ({match.schema})")
        else:
            print(f"  clear    : {item.query!r} - no sanctioned name matched")

    return total


def run(
    datasets: tuple[str, ...] = (DEFAULT_DATASET,),
    refresh: bool = False,
    dry_run: bool = False,
    names: tuple[str, ...] = (),
    from_ens: bool = False,
    address: str | None = None,
) -> int:
    name_mode = bool(names or from_ens)
    if address and not (name_mode and len(names) == 1):
        print("error: --address attaches one --name, pass exactly one", file=sys.stderr)
        return 2

    db = init_db()
    try:
        targets = load_targets(db)
        queries = gather_name_queries(db, names, from_ens, address) if name_mode else []

        if name_mode and not queries:
            print("no ENS name is cached in intel.db, and no --name was given")
            print("hint: run run_intel.py first, or pass --name \"Some Entity\"")
            return 1
        if not name_mode and not targets:
            print("no target wallets cached in intel.db")
            print("hint: run run_intel.py first, or seed the wallets table")
            return 1

        if not name_mode:
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

            if name_mode:
                total_matches += screen_names(db, dataset, queries, refresh=refresh, dry_run=dry_run)
            else:
                print(
                    f"  export   : {dataset.entities:,} entities, {dataset.wallets:,} sanctioned wallets"
                    f" ({dataset.size / 1_048_576:.1f} MB, updated {dataset.updated or 'unknown'})"
                )
                matches = match_targets(targets, dataset)

                if matches:
                    stored = 0 if dry_run else store_matches(db, matches, name)
                    for target, wallet in matches:
                        print(f"  MATCH    : {target} -> {finding_text(wallet, name)}")
                    print(f"  matched  : {len(matches)} target(s), {stored} new row(s) stored")
                else:
                    print(f"  matched  : 0 of {len(targets)} target(s) against {dataset.wallets:,} wallets")

                if not dry_run:
                    record_run(db, dataset, checked=len(targets), matches=len(matches))
                total_matches += len(matches)
            print()

        if name_mode:
            print(f"summary: {len(queries)} name(s) screened · {total_matches} match(es)")
        else:
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
        "--name",
        action="append",
        dest="names",
        metavar="NAME",
        help="screen an entity name instead of the wallet addresses (repeatable)",
    )
    parser.add_argument(
        "--from-ens",
        action="store_true",
        help="screen every ENS name cached in intel.db",
    )
    parser.add_argument(
        "--address",
        metavar="ADDR",
        help="wallet to attach a single --name match to",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-download the export and re-screen names already cached",
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
        names=tuple(args.names or ()),
        from_ens=args.from_ens,
        address=args.address,
    )


if __name__ == "__main__":
    raise SystemExit(main())
