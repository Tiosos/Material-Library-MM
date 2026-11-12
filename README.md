# Material Library - MM

> Universal Material Library — a **material master-data (MDM) solution independent of any business system** plus a runnable prototype.
> It can stand alone as a query site, or be referenced / imported by joineryflow or other systems.

*A vendor-neutral material master-data library with forgiving, "type-half-of-it-and-still-find-it" search. Ships as a spec (schema + mapping) plus a zero-dependency prototype.*

Reference source: [`github.com/Tiosos/joineryflow`](https://github.com/Tiosos/joineryflow) (a joinery production system; its Catalog module has 9 material tables and 14 product types).

---

## Contents

```
.
├── README.md                    this file
├── LICENSE                      MIT
├── docs/design-v1.md            ★ the design document (read this)
├── schema/
│   ├── material.sql            canonical DDL (PostgreSQL, with weighted tsvector + pg_trgm)
│   ├── search.sql              search / facets / suggest / paged payload functions
│   ├── api.sql                 read-only payload functions (detail, family, changes, export, measures)
│   ├── merge.sql               duplicate scan / preview / merge / ignore (T2)
│   ├── writeback.sql           approval state machine + write-back mapping projections (T3)
│   └── material.schema.json    UMR exchange-format JSON Schema (contract)
├── api/                        read-only HTTP API (FastAPI, T1) plus five review routes (T3)
├── eval/                       golden search cases + deterministic evaluation fixture
├── scripts/eval_search.py      search evaluation runner (MRR@10, hit@1, zero-result, level accuracy)
├── tests/                      HTTP smoke tests, ETL and write-back integration tests (pytest)
├── mapping/
│   └── joineryflow.yaml        executable v2 rules: 9 catalog tables → UMR (ETL step 1)
├── fixtures/
│   └── joineryflow-hartwood.jsonl   committed 18-row snapshot, the ETL's only input
├── scripts/export_joineryflow.py    read-only exporter for that snapshot (step 0)
├── scripts/check_joineryflow_mapping.py  static mapping v2 validator
├── scripts/import_joineryflow.py    snapshot import CLI (step 3)
├── scripts/etl_contract.sql         post-import SQL assertions (step 4)
├── scripts/apply_change_requests.py dry-run-first write-back worker (T3 step 3)
├── scripts/writeback_contract.sql   SQL assertions for the T3 state machine (step 4)
├── scripts/prune_change_requests.py dry-run-first retention worker (T3 6.3)
├── ingest/joineryflow.py            pure snapshot reader and draft builder (step 2)
├── docs/etl-joineryflow.md          ETL design (final)
├── docs/etl-joineryflow-progress.md ETL implementation log and verification
├── docs/t3-writeback.md             T3 write-back design (final)
├── docs/t3-progress.md              T3 implementation log and verification
└── proto/                       ★ runnable prototype (zero dependencies, to validate the search feel)
    ├── build_db.py             generate the sample library (100 rows, real brands and field structure)
    ├── search.py               tiered graceful-degradation search implementation
    ├── serve.py                HTTP server (search / detail / export)
    ├── data/materials.jsonl    UMR exchange-format samples
    └── web/index.html          search UI
```

---

## Quick start (prototype)

```bash
cd proto
python3 build_db.py     # generate materials.sqlite + data/materials.jsonl
python3 serve.py 8848   # http://localhost:8848
```

Zero dependencies: only the Python 3 standard library, no pip install needed.

Once open, just search; these queries verify the four-level graceful degradation of "forgiving search":

| Input | Level used | Description |
|---|---|---|
| `blum soft close hinge` | full-text AND | all words must match |
| `blu` | prefix | a partial word still counts |
| `blum hinge 450` | relaxed | automatically drops the words with no hits, no longer returns empty |
| `luminux` | fuzzy | typo → finds Laminex again, and suggests "did you mean laminex" |
| `tempered glass` | synonym | common name → toughened safety glass |
| `thickness:18` | attribute filter | you don't remember the name, only the spec |
| `blum -hinge` | exclusion | exclude a category |
| `mdf 18 white` | full-text AND | mixed search of common name + spec |

The status bar at the top of the interface tells you **which search level was used this time**; that is the key to judging whether the feel is right.

---

## HTTP API

| Endpoint | Description |
|---|---|
| `GET /api/search?q=&category=&brand=&supplier=&limit=` | search, returns the hit list + the degradation level used this time + spelling suggestions |
| `GET /api/material/{uid}` | single record detail (including attributes, price history, substitutes, identifiers) |
| `GET /api/export?q=&format=jsonl\|csv` | export as UMR exchange format or CSV |
| `GET /api/suggest?q=` | input autocomplete |

`GET /api/search` response example (excerpt):

```json
{
  "level": "relaxed",
  "total": 3,
  "suggestion": null,
  "results": [{ "material_uid": "mat_01...", "name": "Blumotion Soft Close Hinge", "score": 7.4 }]
}
```

`level` is the level actually used for this search, and `suggestion` is the "did you mean ___".

---

## Core idea (one sentence)

> Merge joineryflow's **9 material tables into 1 core table + type profiles + attribute KV**, give every material a **permanently stable `material_uid` across systems**, then replace today's single-keyword `ILIKE` with **four-level graceful-degradation search**.

See [`docs/design-v1.md`](docs/design-v1.md).

---

## Data model v2 (PostgreSQL, actually run)

`schema/` holds the v2 model: category tree, supplier entity, units and measures, product family, versions and events, approval-based write-back. For the design notes see [`docs/data-model-v2.md`](docs/data-model-v2.md).

```bash
docker run --rm -d --name ml-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=ml postgres:16-alpine
docker cp schema ml-pg:/schema
docker exec ml-pg psql -U postgres -d ml -v ON_ERROR_STOP=1 \
  -f /schema/material.sql -f /schema/search.sql -f /schema/seed.sql -f /schema/sample_demo.sql
```

The three scripts can be run repeatedly; `sample_demo.sql` includes acceptance queries (five-level search, unit conversion, family aggregation, incremental cursor, event stream, write-back request, hard-delete interception).

- `material.sql` — tables, triggers (search vector / version / events), views
- `search.sql` — `material_search()` five-level graceful degradation, `material_facets()`, `material_search_page()`
- `seed.sql` — category tree / units (UCUM) / 11 field profiles
- `sample_demo.sql` — 3 same-family boards + glass + hardware + duplicate candidates + acceptance output
- `material.schema.json` — v2 exchange contract (with `schema_version`)

---

## Read API v1 (T1)

A read-only HTTP surface over the same SQL functions — the prototype's `serve.py` is not it: this one serves the v2 PostgreSQL model, tells the truth about `total`, filters by physical measures with unit conversion, logs searches, and ships a search-quality gate.

```bash
pip install -r api/requirements.txt
set ML_DSN=postgresql://postgres:postgres@127.0.0.1:55432/ml
uvicorn api.main:app --host 127.0.0.1 --port 8848
```

| Endpoint | Description |
|---|---|
| `GET /healthz` | liveness (checks the database) |
| `GET /v1/meta` | `schema_version`, material count, `max_sync_seq` |
| `GET /v1/materials` | search + browse: `q`, `category`, `brand`, `supplier`, `attrs`, `measures`, `limit`, `cursor`; returns `level`, truthful `total`, `suggestion`, `next_cursor` |
| `GET /v1/materials/{uid}` | full UMR v2 document (`?measures=preferred\|all`) |
| `GET /v1/materials/{uid}/family` | the family (axis values, member count) and its members |
| `GET /v1/facets` | facet counts for the same filters, computed on the filtered hit set |
| `GET /v1/suggest` | prefix suggestion for a search box |
| `GET /v1/changes?since=` | incremental feed on `sync_seq` with `next_since` (no skips) |
| `GET /v1/export?format=jsonl\|csv` | streamed full export; JSONL is one UMR document per line |
| `POST /v1/searches/{log_id}/clicks` | record which hit a user clicked (`{material_uid, rank}`) |

Runtime settings (environment): `ML_DSN` (required), `ML_API_HOST` (127.0.0.1), `ML_API_PORT` (8848), `ML_API_TOKEN` (empty = open), `ML_ACTOR_DEFAULT` (anonymous), `ML_LOG_SEARCH` (1). `X-Actor` names the caller in the search log, `X-No-Log: 1` skips logging for one request.

Search quality is a gate, not an opinion:

```bash
python scripts/eval_search.py --dsn "$ML_DSN" --dataset fixture   # deterministic corpus, five levels
python scripts/eval_search.py --dsn "$ML_DSN" --dataset blum      # real part numbers from the sample import
python -m pytest -q tests                                        # HTTP contract, MCP, mapping/importer and ETL tests
```

The four ETL integration cases in `tests/test_etl.py` skip unless `ML_ETL_DSN` points at a fresh
database that has imported the joineryflow snapshot; the seventeen write-back integration cases in
`tests/test_writeback.py` skip unless `ML_WRITEBACK_DSN` is set. The dedicated `etl` and
`writeback` CI jobs supply those DSNs. On the CI `api` recipe the suite reports
`179 passed, 21 skipped`.

Design and decisions: [`docs/api-v1.md`](docs/api-v1.md) (endpoints, SQL additions, instrumentation, evaluation thresholds). Implementation log with verification for every step: [`docs/t1-progress.md`](docs/t1-progress.md).

---

## MCP server (B8)

Seven read-only tools over the same API, for a model that should search the catalogue instead of guessing: `search_materials`, `get_material`, `get_family`, `list_facets`, `suggest_materials`, `list_changes`, `catalogue_info`, plus a `materials://{uid}` resource for attaching one document to a conversation. No SQL and no business rules — if the API does not answer it, the tool does not either.

```bash
pip install -r mcp_server/requirements.txt
set ML_API_BASE_URL=http://127.0.0.1:8848
python -m mcp_server                 # stdio, the default a desktop host starts
python -m mcp_server --transport http --base-url http://127.0.0.1:8848
```

Host configuration (Claude Desktop, Cursor and others read the same shape):

```json
{
  "mcpServers": {
    "material-library": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {"ML_API_BASE_URL": "http://127.0.0.1:8848"}
    }
  }
}
```

Settings: `ML_API_BASE_URL` (127.0.0.1:8848), `ML_API_TOKEN` (empty), `MCP_ACTOR` (`mcp`, sent as `X-Actor` so the search log shows which client asked), `MCP_TIMEOUT` (10 s), `MCP_TRANSPORT` (`stdio`), `MCP_HTTP_HOST` (127.0.0.1), `MCP_HTTP_PORT` (8849). A failed call comes back as a tool error carrying the API's own `code` and sentence, so the model can fix its arguments instead of retrying.

Design and decisions: [`docs/mcp-server.md`](docs/mcp-server.md). Implementation log with the SDK entry points and the verification of every step: [`docs/mcp-progress.md`](docs/mcp-progress.md).

---

## Importing supplier catalogues (B9)

Without changing the supplier's table, give each vendor a `mappings/<vendor>.yaml` (free-form header → UMR fields):

```bash
pip install -r ingest/requirements.txt

# 1) Without Excel, first extract the PDF catalogue into a table (layout-specific, the Blum hinge section is verified)
python -m ingest.pdf_blum "F:/.../Blum Catalogue and technical manual 2024-2025.pdf" \
       --pages 80-95 -o ingest/samples/blum_hinges.csv

# 2) dry-run: see the header matching and family assignment results, without connecting to the database
python scripts/import_file.py --file ingest/samples/blum_hinges.csv --mapping mappings/blum.yaml

# 3) write to the database
set ML_DSN=postgresql://postgres:postgres@127.0.0.1:55432/ml
python scripts/import_file.py --file ingest/samples/blum_hinges.csv --mapping mappings/blum.yaml --dsn %ML_DSN% --apply
```

For mapping syntax, header matching order, and idempotency rules see [`docs/import-pipeline-b9.md`](docs/import-pipeline-b9.md). Measured: 122 rows of real Blum catalogue data → 105 materials (a re-run adds 0); three Laminex thicknesses → 1 family with 3 records.

Every mapping is validated against [`schema/mapping.schema.json`](schema/mapping.schema.json). The CI `static` job runs `scripts/check_mappings.py` (the schema plus cross-field rules such as template placeholders and family keys naming real fields) together with the pure-Python unit tests for `ingest.mapping` and `ingest.importer`. Both run locally without a database:

```bash
python scripts/check_mappings.py
python -m unittest discover -s tests -p "test_unit_*.py" -v
```

A second worked example imports a joinery procurement sheet with parsed sheet sizes and thickness-based families: see [`docs/25haw-orders-import.md`](docs/25haw-orders-import.md), [`mappings/25haw_orders.yaml`](mappings/25haw_orders.yaml) and the redacted sample [`ingest/samples/25haw_orders_redacted.csv`](ingest/samples/25haw_orders_redacted.csv). Measured: 71 real material rows → 71 materials, 96 measures and 23 families; a re-run adds 0. The committed sample contains no prices, people, project name or location, order data, dates, quantities or attachment references. CI asserts these numbers in the `database` job.

---

## Real-data ETL (snapshot-first, joineryflow)

The first real catalogue is joineryflow's nine material tables, imported through a **committed
JSONL snapshot** rather than a live connection: CI never connects to the source system, and a
byte-identical re-export is the acceptance criterion.

```bash
pip install -r ingest/requirements.txt

# 1) Pure verification, no database: read the snapshot and build 18 Drafts
python -m ingest.joineryflow --dry-run --json

# 2) Static check that mapping v2 matches the snapshot, schema and seed data
python scripts/check_joineryflow_mapping.py

# 3) Dry-run report from the import CLI
python scripts/import_joineryflow.py --dry-run --json report.json

# 4) Write to a fresh PostgreSQL 16 database (schema + search + seed loaded first)
set ML_DSN=postgresql://postgres:postgres@127.0.0.1:5432/ml
python scripts/import_joineryflow.py --dsn %ML_DSN% --json first.json

# 5) Assert the imported state (use docker exec ... psql if PG runs in a container)
psql -v ON_ERROR_STOP=1 -q -d ml -f scripts/etl_contract.sql
set ML_ETL_DSN=%ML_DSN%
python -m pytest -q tests/test_etl.py --noconftest
```

Measured on a fresh database: 18 materials, 0 archived, 6 categories (`board:10 hardware:4
benchtop:1 appliance:1 custom:1 hire:1`), 17 single-member families, 10 derived `thickness_mm`
measures plus one from a real column, 13 prices, 2 `cv_code` identifiers and `source_payload`
on all 18 rows. `material_search('18mm')` finds both 18 mm boards. A second import reports
`new = 0, updated = 18`, and `rev` stays 1. The dedicated `etl` CI job rebuilds all of this from
scratch on every PR using its own `ml_etl` database and `ML_ETL_DSN`.

Design (final): [`docs/etl-joineryflow.md`](docs/etl-joineryflow.md). Verification log, decisions
and the three numbers that differ from the design prose:
[`docs/etl-joineryflow-progress.md`](docs/etl-joineryflow-progress.md).

---

## Write-back executor (T3)

Approved changes can be written back to joineryflow, but **only through an explicit worker**. The
HTTP API can file, list, inspect, approve and reject a `material_change_request`; it never sends a
PATCH and there is no `/apply` route. CI never egresses to the source system either:
`tests/test_writeback.py` starts a stdlib `http.server` on 127.0.0.1 with an ephemeral port and a
dummy token.

```bash
# 1) Load the state machine after seed.sql (it references material_category rows)
psql -q -v ON_ERROR_STOP=1 -d ml -f schema/writeback.sql

# 2) Review routes (FastAPI; X-Actor names the caller)
#    POST /v1/change-requests, GET /v1/change-requests,
#    GET /v1/change-requests/{id}, POST .../approve, POST .../reject

# 3) Dry-run is the default: zero writes, zero events, zero HTTP calls
python scripts/apply_change_requests.py --dsn "$ML_WRITEBACK_DSN" --dry-run --json -

# 4) A real PATCH needs explicit configuration (missing settings exit 2 before any DB write)
#    Use the local fake server's URL in tests and the real base URL in production.
set ML_JF_BASE_URL=http://127.0.0.1:PORT
set ML_JF_TOKEN=...
python scripts/apply_change_requests.py --dsn "$ML_WRITEBACK_DSN" --apply --json report.json

# 5) Assert the state machine on a fresh database; no upstream credentials needed
set ML_WRITEBACK_DSN=postgresql://postgres:postgres@127.0.0.1:5432/ml_writeback
psql -q -v ON_ERROR_STOP=1 -d ml_writeback -f scripts/writeback_contract.sql
python -m pytest -q tests/test_writeback.py --noconftest
```

All five statuses are owned by SQL: `submit`, `decide`, `claim`, `settle` and `retry`. A claim
uses `FOR UPDATE SKIP LOCKED` with a five-minute lease; `before` and the field-level `base`
fingerprint are computed server-side; a stale approval is refused at approve and becomes
`failed / applied_unverified` at apply. A 5xx or timeout becomes `failed / uncertain_outcome` and
is never retried automatically. `Idempotency-Key: mlcr-{id}` is sent but not relied upon, and the
token is never printed or written to a report.

Merge and write-back cannot both be in flight: a merge is refused while a `pending` or `approved`
request names either side of the pair (blocker `open_change_request`), and a request whose material
was merged away or archived can no longer be approved, planned, applied or retried - it ends as
`failed / material_inactive` without a PATCH. A tombstone keeps its field values, so the
field-level `base` fingerprint alone would not have caught it.

A confirmed write is written home. `settle('applied')` applies the diffed fields to the master row
in the same transaction (triggers produce the revision and the event), so reads are correct
immediately, the same diff returns `no_change` instead of being filed twice, and `after: null`
clears a field. An import of a snapshot exported before an applied request is refused rather than
allowed to revert it.

Retention cannot weaken that guard. Every 2xx advances a dedicated upstream-write watermark
(`material_writeback_state`), including the `applied_unverified` cases where the PATCH landed but
the local row was not written; the importer compares `snapshot.exported_at` against the watermark,
not against live request rows. `scripts/prune_change_requests.py` is dry-run by default, deletes
only `applied` and `rejected` after 90 days, and never touches the watermark or `material_event`.

Measured on a fresh PostgreSQL 16 database: `scripts/writeback_contract.sql` green with zero
residual rows, `tests/test_writeback.py` 25/25 with `ML_WRITEBACK_DSN`,
`tests/test_writeback_api.py` 12/12 in the `api` job, and the full suite
`179 passed, 21 skipped` on the CI `api` recipe. The dedicated `writeback` CI job rebuilds this
from scratch on every PR with its own `ml_writeback` database; no `ML_JF_*` credential is set
there.

Design (final): [`docs/t3-writeback.md`](docs/t3-writeback.md). Verification log and decisions:
[`docs/t3-progress.md`](docs/t3-progress.md).

---

## Changing the prototype data

Edit the material pool at the top of `proto/build_db.py` (`BOARDS` / `HARDWARE` / `EDGING` / `GLASS` / `TIMBER` / `BENCHTOP` / `APPLIANCE` / `CUSTOM` / `HIRE` / `CONSUMABLE`), then re-run `python3 build_db.py`.

`SYNONYM_HINTS` is the synonym table — it is what lets "forgiving search" catch common names and Chinese.

---

## Current status

- [x] Design v1 (data model / search / integration / roadmap)
- [x] Data dictionary: DDL + JSON Schema
- [x] joineryflow mapping spec
- [x] Runnable search prototype (SQLite FTS5, 100 sample records)
- [x] **Data model v2**: category tree / supplier entity / units and measures / product family / version events / approval write-back / dedupe candidates / EPD slots (verified on PostgreSQL 16)
- [x] **Search v2**: five-level graceful degradation (exact / prefix / and / relaxed / fuzzy) + filter push-down + facet narrowing (accepted at the SQL layer)
- [x] **Import pipeline v1**: CSV/XLSX free-form header mapping + PDF catalogue extraction (run on 122 rows of real Blum data, idempotent)
- [x] **Read API v1 (T1)**: 10 read-only endpoints over PostgreSQL, unit-aware measure filters, truthful `total` with cursor paging, search logging + click tracking, JSONL/CSV export, pytest contract tests, CI job `api` and a search evaluation gate (fixture + Blum catalogue, MRR@10 1.000 on the holdout split)
- [x] **Duplicate merge (T2)**: scan for candidate pairs, review queue, preview, merge with an explicit keeper (survivor becomes a superseded tombstone), ignore; HTTP routes and CLIs, contract assertions and pytest coverage in the `api` job
- [x] **Read API v1b (T1b)**: families collapse to their best hit with `variant_count`/`variants` (`expand=variants` returns the records), and `format=json` exports the versioned envelope (`{schema_version, exported_at, count, records}`) validated by `schema/export.schema.json`
- [x] **MCP server (B8)**: seven read-only tools plus a `materials://{uid}` resource over the read API, stdio by default and streamable HTTP on request, with the API's errors passed through verbatim
- [x] **Real-data ETL (snapshot-first)**: committed 18-row joineryflow snapshot, executable mapping v2, pure reader/draft builder, idempotent import CLI, SQL contract, integration tests and a dedicated `etl` CI job (PRs #9-#13), verified end to end on PostgreSQL 16
- [x] **Write-back executor (T3)**: SQL-owned approval state machine with a five-minute lease and `FOR UPDATE SKIP LOCKED`, dry-run-first PATCH worker with a fake upstream in CI, five HTTP review routes, SQL contract and a dedicated `writeback` CI job

**Next step: operational hardening, not another structural gap.** The model layer now covers design, search, supplier imports, duplicate merge, MCP, real-data ETL and approval-based write-back, and the three T3 consistency follow-ups are merged (PR #17). What remains depends on the live source system: incremental sync needs joineryflow's `updated_at` and real credentials, while `Idempotency-Key` support, the exact workspace guard and resolving `applied_unverified` with a `--verify` GET pass are open questions recorded in [`docs/t3-writeback.md`](docs/t3-writeback.md) §16 and [`docs/t3-progress.md`](docs/t3-progress.md). Request retention is no longer open: the 90-day prune CLI and the watermark are shipped. Who schedules the worker and the prune job is an operations choice. ETIM/eCl@ss codes and EPD collection stay reserved slots until real supplier classification data arrives. T1b closed the two gaps the read API used to advertise: families collapse by default (`expand=variants` returns the records) and `format=json` exports the versioned envelope validated by `schema/export.schema.json`. `docs/t2-merge.md` §9 explains recovery without an `unmerge()`.

---

## License

[MIT](LICENSE) — the design documents, data model, and prototype code are all free to use and modify.

Brand names in the prototype sample data (Blum, Hettich, Laminex, Caesarstone, etc.) are used only to demonstrate field structure and search behaviour; they are unrelated to their respective trademark holders and contain no real prices or commercial terms.
