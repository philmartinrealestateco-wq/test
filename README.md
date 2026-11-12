# Wallet Balance Analyzer

Read-only, multi-chain analysis of public wallet addresses. It answers one
question per address: **does this wallet hold between $50,000 and $900,000 of its
native coin (plus, on BNB Smart Chain, any BEP-20 tokens from a configured
allowlist), and has it transacted recently?**

Supported chains: Bitcoin (BTC), Ethereum (ETH), BNB Smart Chain (BNB), Solana (SOL).

Nothing here can move funds. The tool never asks for a private key, seed phrase
or exchange credential, only reads public chain data over documented APIs, and
marks any value it could not confirm against a blockchain source as
`UNVERIFIED` instead of guessing.

---

## 1. Quick start (Windows)

```bat
cd C:\Users\HP Spectre\wallet-analyzer
.venv\Scripts\activate
python -m wallet_analyzer analyze --input data\addresses.sample.csv
```

From a clean checkout instead of this prepared folder:

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m wallet_analyzer analyze --input data\addresses.sample.csv
```

A single address, without a file:

```bat
python -m wallet_analyzer analyze --address bitcoin:bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4
python -m wallet_analyzer analyze --address 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045
```

The second form is expanded to **both** Ethereum and BNB Smart Chain, because the
two chains share one address space and the balances are unrelated.

---

## 2. Configuration

All settings are optional; the tool runs with no keys at all. Copy `.env.example`
to `.env` and edit.

| Variable | Default | Meaning |
|---|---|---|
| `ETHERSCAN_API_KEY` | *(empty)* | Etherscan V2 unified key. Free tier covers Ethereum only. |
| `BSCSCAN_API_KEY` | *(empty)* | BscScan key used for BNB Chain history through Etherscan V2. Falls back to `ETHERSCAN_API_KEY`. |
| `BLOCKCHAIN_EXPLORER_API_KEY` | *(empty)* | Optional Blockchain.com `api_code`. The explorer works without it, which is the default. |
| `SOLANA_RPC_URL` | `https://api.mainnet-beta.solana.com` | Any Solana JSON-RPC endpoint. |
| `BSC_RPC_URL` | `https://bsc-dataseed.binance.org` | BNB Chain JSON-RPC endpoint. |
| `MIN_USD` | `50000` | Lower balance bound. |
| `MAX_USD` | `900000` | Upper balance bound. |
| `ACTIVITY_DAYS` | `30` | Activity window. |
| `MAX_WORKERS` | `4` | Parallel lookups (per-host rate limits still apply). |
| `REQUEST_TIMEOUT` | `20` | Per-request timeout, seconds. |
| `PRICE_CACHE_TTL` | `300` | Seconds a price snapshot stays fresh. |
| `ALLOW_KEYLESS` | `true` | When `false`, a missing Etherscan key is a hard error. |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every retry and rate-limit wait. |

CLI flags (`--min-usd`, `--max-usd`, `--activity-days`, `--workers`, `--out`)
override the environment for that run.

Credentials are never echoed. The CLI reports `configured`/`absent` only, RPC
endpoints are shown by host, and every provider message passes through
`net.scrub()`, which removes URL paths, query parameters, JWTs and UUID keys
before anything reaches a terminal, log or report.

`SOLANA_RPC_URL` and `BSC_RPC_URL` must be absolute URLs (`https://…`). A bare
API key in those variables is rejected before any request is made, with a
message that does not repeat the value.

### What each key actually unlocks

| Setup | Ethereum | BNB Smart Chain | Bitcoin | Solana |
|---|---|---|---|---|
| **No key (default)** | Blockscout, full history | native + BEP-20 balances → `PARTIAL` | full | full (public RPC) |
| **Etherscan free key** | Etherscan V2, full history | native + BEP-20 balances → `PARTIAL` | full | full |
| **Etherscan paid key** | Etherscan V2 | Etherscan V2, full history | full | full |
| **+ private Solana RPC** | — | — | — | higher rate limits |

This is not a configuration choice; it is how the providers behave:

* `api.bscscan.com` now returns `301` to `docs.etherscan.io/v2-migration`. The
  legacy standalone BscScan API no longer exists.
* Etherscan V2 responds to `chainid=56` on a free plan with
  `Free API access is not supported for this chain`. A paid plan is the only
  documented way to read BNB Chain history.
* Blockscout publishes an Etherscan-compatible API for Ethereum without a key,
  which is why keyless Ethereum is fully usable while keyless BSC is not.

Run `python -m wallet_analyzer providers` to print the active routing.

### Checking connections

```bat
python -m wallet_analyzer doctor
python -m wallet_analyzer doctor --json
```

`doctor` performs one real, read-only request per service against a well-known
public address and prints a table of `OK` / `NEEDS CONFIG` / `FAILED` with the
reason and a suggested next step. It never prints a credential.

Exit code is `0` when every *required* service works. BNB Chain activity is
optional (see below), so a BSC-only gap does not fail the command; a broken
Bitcoin, Ethereum, Solana or price connection does.

---

## 3. Input formats

**CSV / TSV** — an `address` column is required; `network` and `label` are
optional. Header matching is case-insensitive and accepts `wallet_address`,
`chain`, `blockchain`, `label`, `name`, `note` as aliases.

```csv
address,network,label
bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97,bitcoin,treasury
0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045,,both-evm-chains
9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM,solana,hot-wallet
```

**TXT** — one address per line, optional `network:` prefix, `#` starts a comment.

```
# payroll wallets
bitcoin:bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4
solana:9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM
```

Network values: `bitcoin`/`btc`, `ethereum`/`eth`, `bsc`/`bnb`, `solana`/`sol`.

Rows without a network are detected from the address format. Addresses that
match no known format are reported as warnings and skipped; addresses that name
a chain but fail validation appear in the report as `INVALID_ADDRESS`.

---

## 4. What counts as a match

A wallet is reported in the matches list only when all three hold:

1. **Balance verified** against a chain-backed source (`VERIFIED`, not `PARTIAL`).
2. **Active** — at least one confirmed transaction inside the activity window,
   measured at run time.
3. **USD value inside the configured range**, inclusive.

A positive balance never implies activity: a wallet funded three years ago and
untouched since is `INACTIVE`.

On BNB Smart Chain the range check uses **Total USD** = native BNB + priced
BEP-20 holdings from the configured allowlist. Everywhere else Total USD equals
the native value.

### BEP-20 token coverage (BNB Smart Chain)

Balances are read with `eth_call` against each token contract:

| Call | Selector | Purpose |
|---|---|---|
| `balanceOf(address)` | `0x70a08231` | token balance of the wallet |
| `decimals()` | `0x313ce567` | scale factor for the raw integer |
| `symbol()` | `0x95d89b41` | display symbol (ABI string or legacy `bytes32`) |

Requests are batched into one JSON-RPC array per wallet, with automatic fallback
to single calls if a node rejects batches. `decimals`/`symbol` are resolved once
per contract and cached for the run.

Valuation uses CoinGecko's `simple/token_price/binance-smart-chain` endpoint. The
free tier accepts **one contract address per request** (`error_code 10012`), so
prices are fetched only for contracts a wallet actually holds, one call each,
cached for `PRICE_CACHE_TTL`. Each contract is retried independently with
exponential backoff (`429`/`5xx` always, plus `400` for this endpoint); a
contract that stays unavailable is recorded as missing and cannot discard prices
already fetched for the others. A holding with no price is never valued at zero:
it makes the wallet's token valuation incomplete, which forces `PARTIAL`.

This is an **allowlist, not discovery**. The default list is thirteen major tokens
(`wallet_analyzer/tokens.py`); add your own with `--token` or `--token-file`.
Determining every token a wallet holds requires a log indexer, which public RPC
does not provide — see §10.

### Verification states

| Status | Meaning |
|---|---|
| `VERIFIED` | Native balance, activity and (on BSC) token valuation are all complete. |
| `PARTIAL` | Balance confirmed but something else is not: activity unavailable, a token contract unreadable, or a holding unpriced. |
| `UNVERIFIED` | The lookup failed; the CSV keeps the reason and leaves the value blank. |
| `INVALID_ADDRESS` | Rejected before any network call; the reason is recorded. |

An incomplete token valuation can only ever downgrade a wallet: `PARTIAL` is the
ceiling until every checked contract is readable and priced.

A failed lookup is never rendered as `0`. `0` always means the chain said zero.

### Activity states

`ACTIVE` (tx inside the window), `INACTIVE` (verified, nothing recent),
`UNKNOWN` (activity could not be verified).

---

## 5. Outputs

Every run writes timestamped files (UTC) to `out/`:

| File | Contents |
|---|---|
| `wallet_matches_<stamp>.csv` | Only matching wallets, sorted by USD descending. |
| `wallet_analysis_<stamp>.csv` | Every analysed address/network pair. |
| `wallet_analysis_<stamp>.xlsx` | Sheets: Matches, All Addresses, Summary, Network Distribution. |
| `dashboard_<stamp>.html` | Single-file dashboard; no server, no CDN, opens offline. |

Columns (required order, plus token columns and `Notes`):

```
Wallet Address | Blockchain Network | Native Token | Native Token Balance | USD Balance |
Last Transaction Date | Days Since Last Transaction | Activity Status | Data Source |
Verification Status | Token Holdings (USD) | Total USD | Token Count | Notes
```

`USD Balance` is always the native coin. `Token Holdings (USD)` is blank when the
token layer did not run; `Total USD` falls back to the native value in that case.
`Notes` lists the largest BEP-20 holdings and the allowlist disclaimer.

The CSV is the precision-preserving artifact (balances keep full decimal
precision). The workbook converts balances to floats for spreadsheet maths and,
because Excel stores doubles, values above ~15 significant digits lose precision
there. Use the CSV when precision matters.

`Data Source` records exactly which API produced each number, and `Notes` records
why a wallet is not `VERIFIED`.

---

## 6. Data sources

| Chain | Endpoint | Key | Used for |
|---|---|---|---|
| Bitcoin | `blockchain.info/rawaddr/{addr}?limit=1` | no | balance (satoshi), `n_tx`, newest tx time |
| Ethereum | `eth.blockscout.com/api` (Etherscan-compatible) | no | balance, `txlist` page 1 |
| Ethereum / BSC | `api.etherscan.io/v2/api` | yes | balance, `txlist` |
| BNB Smart Chain | public JSON-RPC `eth_getBalance` (configured endpoint, then the defibit/ninicoin dataseeds) | no | native BNB balance only |
| BNB Smart Chain tokens | public JSON-RPC `eth_call` (`balanceOf`, `decimals`, `symbol`) | no | BEP-20 balances from the allowlist, batched |
| BEP-20 prices | `api.coingecko.com/api/v3/simple/token_price/binance-smart-chain` | no | USD per token contract, one contract per request |
| Solana | JSON-RPC `getBalance`, `getSignaturesForAddress` | no | lamports, newest successful signature |
| Token holders | `api.etherscan.io/v2/api` `module=token&action=tokenholderlist` | yes (Pro) | discovery only; CSV fallback otherwise |
| Prices | `api.coingecko.com/api/v3/simple/price` | no | BTC/ETH/BNB/SOL in USD |
| Prices (fallback) | `api.coinbase.com/v2/prices/{PAIR}-USD/spot` | no | per-asset spot |

If no price source is reachable and no cached snapshot exists, the run aborts with
exit code `3` rather than valuing wallets at an invented price.

---

## 7. Architecture

```
wallet_analyzer/
  config.py        settings from .env, threshold validation, redacted summary
  models.py        Network, WalletRecord, AnalysisSettings, AnalysisResult
  crypto.py        Keccak-256, base58, bech32 (no Web3 dependency)
  validators.py    per-chain validation, EIP-55, network detection
  inputs.py        CSV/TSV/TXT/CLI parsing, ambiguity expansion
  net.py           shared session, per-host rate limiters, retries, scrubbing
  prices.py        CoinGecko + Coinbase, TTL cache, stale fallback
  analyzer.py      dedupe → validate → fetch → value → classify → filter
  report.py        CSV + styled XLSX
  dashboard.py     self-contained HTML
  cli.py           analyze / prices / providers / validate
  providers/
    base.py        Provider contract, JSON-RPC client
    bitcoin.py     blockchain.com
    evm.py         Etherscan V2, Blockscout, BSC (RPC balance + indexed activity)
    solana.py      Solana JSON-RPC
```

Design notes:

* **`web3.py` and `solana-py` are deliberately absent.** Both would still need a
  RPC endpoint, and neither is required for `eth_getBalance` or
  `getSignaturesForAddress`. Keeping raw JSON-RPC means no large dependency tree
  and everything stays testable with a stubbed HTTP client.
* **Rate limiting is per host, shared across worker threads.** Raising
  `--workers` adds concurrency without increasing the request rate any provider
  sees. Current sustained intervals: Etherscan 0.25 s, Blockscout 0.25 s,
  blockchain.com 1.0 s, Solana public 0.7 s, CoinGecko 2.0 s.
* **Retries** cover `429/5xx` and transport errors with exponential backoff plus
  jitter, honouring `Retry-After`. Token-price calls opt into retrying `400` as
  well, since CoinGecko returns that status for throttling-adjacent rejections of
  a single contract; a `400` on any other endpoint is still treated as final.
* **Secrets never leave `net.py`'s boundary**: exception messages are scrubbed of
  `apikey=`-style parameters, logging records hosts rather than URLs, and
  `Settings.public_summary()` exposes only whether a key is present.

---

## 8. CLI reference

```
python -m wallet_analyzer analyze [options]
    --file PATH             TXT, one address per line: bulk mode (see below)
    --input PATH            CSV/TSV/TXT file
    --address ADDR          repeatable; accepts network:address
    --network NAME          force a network for --address values
    --min-usd / --max-usd   override the balance range
    --activity-days N       override the activity window
    --workers N             1-32 parallel lookups
    --out DIR               output directory (default: ./out)
    --no-dashboard          skip the HTML file
    --force-prices          bypass the price cache
    --quiet                 only print the final summary
    --filter                with --file: also write <out>/report_matches.csv
    --no-tokens             skip BEP-20 balances (native coin only)
    --token 0xADDR[:SYM[:D]] extra BEP-20 contract to check; repeatable
    --token-file PATH       CSV of contracts (contract[,symbol[,decimals]])

python -m wallet_analyzer prices [--force]
python -m wallet_analyzer providers
python -m wallet_analyzer doctor [--json]
python -m wallet_analyzer discover --token 0x... [--chain bsc|ethereum] [--csv PATH] [--out PATH]
python -m wallet_analyzer validate --network ethereum --address 0x...

scan_target.bat <token> [chain] [limit] [csv-fallback]   # discover + analyze --filter
```

### Bulk mode (`--file`)

```bat
python -m wallet_analyzer analyze --file data\wallets.sample.txt
```

Reads a `.txt` with one address per line (blank lines and `#` comments are
ignored) and processes each wallet independently:

```
bulk mode: 3 wallet(s) from wallets.sample.txt
report: C:\...\out\report.csv (appending)

[1/3] Processing bc1qw508d6..7kv8f3t4
[2/3] Processing 0xd8dA6BF2..7aA96045
[3/3] Processing 9WzDXwBbmk..9zYtAWWM

bulk complete: 4 row(s) across 3 wallet(s)
```

Results are appended to `<out>/report.csv`:

| Wallet | Network | Native USD | Token USD | Total USD | Status |
|---|---|---|---|---|---|

* One row per wallet/network pair. A bare `0x` address is analysed on Ethereum
  and BNB Smart Chain, so it yields two rows unless `--network` narrows it — the
  `Network` column keeps them distinguishable.
* The header is written only once, when the file is new; existing rows are never
  rewritten, so an interrupted batch keeps its completed work.
* Token prices come from the shared process cache, so each contract is priced at
  most once per batch even if the TTL would expire mid-run.
* A wallet that fails outright is logged and recorded with `Status = ERROR` and
  blank USD cells; the batch continues with the next one.
* `--file` and `--input` are mutually exclusive: `--input` runs a single batch and
  writes the full timestamped reports, `--file` streams rows incrementally.
* Every run ends with a **Target Matches** section listing the rows whose
  `Total USD` is inside `MIN_USD`–`MAX_USD` (inclusive, highest first). Rows with
  an unknown total — an incomplete valuation — are never listed.
* `--filter` additionally writes those rows to `<out>/report_matches.csv`, using
  the same six columns and the same header-once append behaviour.

```
bulk complete: 16 row(s) across 10 wallet(s)
  report  : C:\...\out\report.csv
  matches : C:\...\out\report_matches.csv

Target Matches (50,000 - 900,000 USD): 2
  0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D  BNB Smart Chain    457,104.25  PARTIAL
  0x267be1C1D684F78cb4F6a176C4911b741E4Ffdc0  Ethereum           371,439.80  VERIFIED
  written to C:\...\out\report_matches.csv
```

### Discovering holders (`discover`)

Address discovery cannot use `eth_getLogs` (public nodes reject it), so this
command populates a holder list from an indexed source instead:

```bat
python -m wallet_analyzer discover --token 0x55d398326f99059fF775485246999027B3197955
python -m wallet_analyzer discover --token 0x55d3... --csv data\dune_holders_sample.csv
```

It calls Etherscan V2 `module=token&action=tokenholderlist` with
`ETHERSCAN_API_KEY`. **That endpoint is Pro-only on both chains** — BSC answers
"Free API access is not supported for this chain", Ethereum answers "API Pro
endpoint" — so with a free key the command falls back to `--csv`: it reads wallet
addresses from the **first column** of a local export (Dune and similar) and skips
anything that is not a 20-byte hex address, which drops headers and notes
automatically. Duplicate addresses are collapsed and every address is written in
EIP-55 form.

```
contract: 0x55d398326f99059fF775485246999027B3197955
source  : CSV fallback (dune_holders_sample.csv)
holders : 3
written : out\holders_usdt.txt
next    : python -m wallet_analyzer analyze --file out\holders_usdt.txt
```

* `--chain` selects `bsc` (chainid 56, default) or `ethereum` (1).
* `--limit` requests N holders (default 100, maximum 1000).
* `--out` defaults to `<out>/holders_<contract>.txt`; the file is one address per
  line with no header, so it is valid `--file` input as-is.
* With no `--csv` and an unavailable API, the command exits `2` and repeats the
  provider's reason plus the hint to pass `--csv`.

### One-shot scan (`scan_target.bat`)

Chains discovery into analysis so one command covers the whole pipeline:

```bat
scan_target.bat 0x55d398326f99059fF775485246999027B3197955
scan_target.bat 0x55d398326f99059fF775485246999027B3197955 bsc 1000 data\dune_holders_sample.csv
```

1. `discover --token <token> --chain <chain> --limit <limit> --out <out>\holders_<token>.txt`
2. `analyze --file <that file> --filter`

| Argument | Default | Meaning |
|---|---|---|
| `token` | required | ERC-20/BEP-20 contract address |
| `chain` | `bsc` | `bsc` or `ethereum`, used for discovery |
| `limit` | `1000` | holders requested |
| `csv-fallback` | none | local export used when the API is unavailable |

Outputs: `out\holders_<token>.txt` (the `--file` input), `out\report.csv` (all
rows) and `out\report_matches.csv` (rows inside the target range).

On a free Etherscan plan step 1 fails because `tokenholderlist` is Pro-only, and
the script **stops before analysing** rather than scanning an empty list — pass
the CSV export as the fourth argument. Because the analysis step auto-detects
networks, each `0x` holder is evaluated on both Ethereum and BNB Smart Chain.

Exit codes: `0` success, `1` invalid address (`validate`) or a required service
down (`doctor`), `2` configuration or input error, `3` no price feed, `130`
interrupted. `scan_target.bat` exits `2` when discovery fails and `3` when the
analysis step fails.

---

## 9. Tests

```bat
.venv\Scripts\python.exe -m pytest
```

236 tests, no network access required. Covered: Keccak-256/EIP-55 published
vectors, base58check, bech32/bech32m, address validation, input parsing,
provider payload handling for all four chains, analyzer filtering and failure
semantics, CSV/XLSX contents, price fallback and caching, dashboard escaping,
credential resolution and secret scrubbing, the `doctor` status rules, the
BEP-20 layer (selectors, ABI string/bytes32 symbol decoding, decimals bounds,
batched scans with single-call fallback, metadata caching, per-wallet token
valuation, PARTIAL downgrade), transport retry behaviour (429 backoff,
persistent-429 exhaustion, default no-retry on 400, opt-in 400 retries,
per-contract price isolation), batch-wide price caching, Solana zero-balance and
malformed-payload handling, bulk mode (file parsing, address grouping, CSV
header-once appending, target-match selection and its filtered file, and
continuing past a failed wallet), and discovery (Etherscan holder payload
parsing, Pro-plan errors, CSV first-column extraction, dedupe, and the
`--file`-ready output format).

The suite is the reason a real bug was caught early: the base58check verifier
initially hashed the checksum together with the payload, which rejected every
valid legacy Bitcoin address.

---

## 10. Limitations

Be aware of these before trusting a number:

1. **BSC without a paid Etherscan key can never produce a match.** Native BNB
   balance is verified through public RPC (with automatic failover across the
   public dataseeds) and BEP-20 balances are read from the allowlist, but
   transaction history is unavailable, so activity stays `UNKNOWN` and the
   wallet is `PARTIAL`. This is a provider limitation, not a bug.
2. **Token coverage is an allowlist, never a discovery.** Only the contracts in
   the configured list are read. A wallet whose value sits in an unlisted token
   looks smaller than it is; the report says so in `Notes` and the dashboard
   footer. There is no way around this without an indexer.
3. **Public BSC RPC cannot serve history.** Measured on `bsc-dataseed` on
   2026-11-11: `eth_getLogs` answers `{"code": -32005, "message": "limit
   exceeded"}` even for a 3-block range filtered to one contract and one address,
   and `eth_getTransactionsByAddress`, `eth_getHistory` and `trace_filter` are
   not exposed (`-32601`/`-32002`). There is no address index, so neither
   historical activity nor token discovery is possible from this endpoint.
   Balance reads (`eth_getBalance`, `eth_call`) work normally.
4. **BEP-20 valuations depend on a free price API.** CoinGecko accepts one
   contract per token-price request on the free tier, so runs with several held
   tokens need several calls at ~2s each. Prices are cached; a token with no
   price leaves the wallet `PARTIAL` rather than valued at zero.
5. **Bitcoin is UTXO-based.** `blockchain.info` reports aggregate unspent output
   value and recent transactions, not an account ledger. Activity is the newest
   transaction in the returned page. The API can return `429` under load; the
   client backs off, but heavy batches are slow by design.
6. **Solana public RPC throttles aggressively.** Only the newest 25 signatures are
   inspected. If all are failed transactions, activity is reported `UNKNOWN`
   rather than assumed inactive. Point `SOLANA_RPC_URL` at a paid endpoint for
   large batches.
7. **Spot valuation at run time.** Balances are multiplied by the current price;
   no historical or time-weighted valuation.
8. **Ethereum history** uses native `txlist`; internal transfers and token
   transfers do not count as activity. Token balances are not read on Ethereum.
9. **One address space, two chains.** A `0x` address is checked on both Ethereum
   and BSC unless you name the network, so identical addresses can appear twice.
10. **This is not chain-wide discovery.** Checking a supplied list of addresses is
    fundamentally different from finding every wallet on BNB Chain: there is no
    enumeration of addresses, no scanning of blocks for new wallets, and no
    ranking of the chain by balance. Only the addresses you provide are examined.
11. **Not an attribution tool.** Nothing here identifies who owns an address.

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Everything `UNVERIFIED`, notes say "unavailable after 4 attempts" | network blocked, or a proxy is required | check connectivity to the host listed in `Data Source`; raise `REQUEST_TIMEOUT` |
| BSC rows are `PARTIAL` | no Etherscan plan covering chainid=56 | expected; see §10.1 |
| `error: no price source reachable` (exit 3) | CoinGecko and Coinbase both failed | retry later, or restore `cache/prices.json` |
| Run is slow | blockchain.com 1 s and Solana 0.7 s per request | use `--workers` with paid RPC endpoints instead |
| `INVALID_ADDRESS` with "EIP-55 checksum mismatch" | mixed-case address with a bad checksum | lowercase it, or fix the typo |
| Excel shows rounded balances | float storage above ~15 digits | use the CSV |

---

## 12. Terms of use

Only public, read-only data is used. Respect each provider's terms and rate
limits; the built-in throttles are conservative but do not grant you a licence.
No authentication, CAPTCHA or access control is bypassed anywhere in this tool.
