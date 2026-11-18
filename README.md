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
│   ├── create-material.sql     approval-based creation of new materials (step 1)
│   └── material.schema.json    UMR exchange-format JSON Schema (contract)
├── api/                        read-only HTTP API (FastAPI, T1) plus five review routes (T3)
├── eval/                       golden search cases + deterministic evaluation fixture
├── scripts/eval_search.py      search evaluation runner (MRR@10, hit@1, zero-result, level accuracy)
├── tests/                      HTTP smoke tests, ETL, write-back and UI shell tests (pytest)
├── api/ui/                     no-build browser UI: shell, one stylesheet, vanilla ES modules
├── mapping/
│   └── joineryflow.yaml        executable v2 rules: 9 catalog tables → UMR (ETL step 1)
├── fixtures/
│   └── joineryflow-hartwood.jsonl   committed 18-row snapshot, the ETL's only input
├── ingest/auto_mapping.py           infer a supplier mapping from CSV/XLSX headers (no YAML)
├── ingest/quality.py                pre-import completeness / duplicate signals (read-only)
├── ingest/price_import.py           price-list import: (supplier, code) join + valid_from history
├── scripts/import_prices.py         CLI for price lists (dry-run / --apply)
├── tests/test_unit_auto_mapping.py  pure tests for header -> mapping inference
├── tests/test_unit_quality.py       pure tests for pre-import quality signals
├── tests/test_unit_table_reading.py pure tests for cover-row / sheet handling
├── tests/test_unit_price_import.py  pure tests for price-list parsing
├── scripts/export_joineryflow.py    read-only exporter for that snapshot (step 0)
├── scripts/check_joineryflow_mapping.py  static mapping v2 validator
├── scripts/import_joineryflow.py    snapshot import CLI (step 3)
├── scripts/etl_contract.sql         post-import SQL assertions (step 4)
├── scripts/apply_change_requests.py dry-run-first write-back worker (T3 step 3, --verify 6.4)
├── scripts/writeback_contract.sql   SQL assertions for the T3 state machine (step 4)
├── scripts/create_material_contract.sql  SQL assertions for create requests (step 1)
├── scripts/prune_change_requests.py dry-run-first retention worker (T3 6.3)
├── scripts/start_ui.py              one-command local UI: build DB + import samples + serve (preview)
├── scripts/build_preview.py         rebuild the static HTML snapshot from the JSON below
├── preview_snapshot.json            exported data behind material-library-preview.html
├── material-library-preview.html    self-contained static snapshot of the UI (double-click to view)
├── ingest/joineryflow.py            pure snapshot reader and draft builder (step 2)
├── docs/etl-joineryflow.md          ETL design (final)
├── docs/etl-joineryflow-progress.md ETL implementation log and verification
├── docs/t3-writeback.md             T3 write-back design (final)
├── docs/t3-progress.md              T3 implementation log and verification
├── docs/ui-bulk-edit.md             manual query and bulk edit UI design (steps 1-7)
├── docs/ui-create-material.md       adding new materials from the UI: design, all 6 steps implemented
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
database that has imported the joineryflow snapshot; the twenty-nine write-back integration cases in
`tests/test_writeback.py` skip unless `ML_WRITEBACK_DSN` is set. The dedicated `etl` and
`writeback` CI jobs supply those DSNs. On the CI `api` recipe the suite reports
`225 passed, 33 skipped`.

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

**No YAML needed for a new supplier** - let the headers infer a mapping, review it, then import:

```bash
# 1) see what was inferred and what would be written (no DB needed)
python scripts/import_file.py --file new_supplier.csv --auto --vendor acme

# 2) optional overrides when the guess needs help (category defaults to 'generic')
python scripts/import_file.py --file new_supplier.csv --auto --vendor acme \
       --category board --unit sheet --brand Acme --code-column "Artikelnummer"

# 3) keep the inferred mapping for repeat imports, then write to the DB
python scripts/import_file.py --file new_supplier.csv --auto --vendor acme \
       --save-mapping mappings/acme.yaml --dsn "$ML_DSN" --apply
```

The inference identifies the code column, price, name/brand/supplier/category/unit/currency columns,
numeric dimension measures (thickness/width/length/angle → mm/deg) and keeps the rest as attributes.
It is deliberately conservative: no family grouping is guessed, and the default category is
`generic`. Every decision is printed for review; `--save-mapping` writes a normal mapping file that
passes the same schema check as hand-written ones. See `ingest/auto_mapping.py` for the rules.

Real supplier files often start with a title/cover block, or are one sheet in a workbook. Use
`--header-row 0` to auto-detect the header row, or `--header-row N` (1-based) to pin it;
`--list-sheets` shows worksheet names and `--sheet "Prices 2026"` picks one. These work with both
explicit and `--auto` mappings:

```bash
python scripts/import_file.py --file supplier.xlsx --list-sheets
python scripts/import_file.py --file supplier.xlsx --auto --sheet "Prices 2026" --header-row 0
```

**Catalogue and prices can be imported separately.** A catalogue import with `--no-price` writes
materials/identifiers/measures but no `material_price` rows; a separate price list is then loaded
with an explicit effective date, joined by `(supplier, code)`:

```bash
python scripts/import_file.py --file catalogue.csv --mapping mappings/acme.yaml --dsn "$ML_DSN" --apply --no-price
python scripts/import_prices.py --file prices.xlsx --sheet "Prices 2026" --header-row 0 \
       --source-system acme_catalogue --valid-from 2026-01-01 --dsn "$ML_DSN" --apply
```

`import_prices.py` never creates materials: an unknown code is reported as unmatched. Prices are
idempotent on `(material, supplier, valid_from, qty_break, cost_basis)` and different dates are
kept as history. `--valid-from` is required (or a per-row date column), so a back-dated list cannot
silently replace the current price. The `material.unit_cost` cache is refreshed only from the
newest price on record. Without `--no-price`, a catalogue import still writes prices as before.

Auto-detection scores the first 25 physical rows for label-like cells and a following data row;
plain CSVs with a header on row 1 stay on row 1. A wrong guess is loud (the inference print shows
the identifier column), and `--header-row N` overrides it.

Every dry run also prints a **data-quality report**: completeness distribution (required fields
from `material_profile`, the same rule as `v_material_completeness`), repeated codes within the
batch, and how many identifiers already exist in the DB (those rows will be updated, not inserted).
With `--dsn` the DB-side checks run; without it only the batch-local checks do. Add `--strict` to
refuse `--apply` when the batch itself has duplicate codes, incomplete rows or an unknown category;
existing-identifier upserts and DB-wide T2 duplicate candidates are informational and never gate a
re-run. The same data lands in the `--json` report under `quality`.

For a permanent vendor, give it a curated `mappings/<vendor>.yaml` (free-form header → UMR fields):

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

# 1b) Approval-based creation, fully shipped (docs/ui-create-material.md, all 6 steps)
psql -q -v ON_ERROR_STOP=1 -d ml -f schema/create-material.sql

# 2) Review routes (FastAPI; X-Actor names the caller)
#    POST /v1/change-requests, GET /v1/change-requests,
#    GET /v1/change-requests/{id}, POST .../approve, POST .../reject
#    Create requests: POST/GET /v1/material-create-requests, GET .../{id},
#    PATCH .../{id} (pending only, author only), POST .../{id}/approve and /reject

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

An unknown outcome can now be settled by evidence. `scripts/apply_change_requests.py --verify N`
GETs the upstream row and reports `upstream_matches`, `upstream_differs` or `unreachable`; it
writes nothing unless `--resolve` is given. A confirmed match turns the request into `applied`,
writes the diffed values and advances the watermark; a confirmed miss records
`verified_not_applied` and becomes retryable; a column the adapter cannot find is `unreachable`
and changes nothing, so a wrong field name can never downgrade a real write.

Every allowed path is now covered by the contract, not just `unit_cost`: the six fields (including
`synonyms` as a `text[]`), the `attributes.<key>` namespace, the four places that derive an upstream
column name asserted against each other, and the tombstone rule - a confirmed write on a merged-away
row lands on the tombstone, never on the survivor. `lead_time_days` was removed from the writable
set: delivery time belongs to a supplier at a point in time, not to a material, so the column and its
ETL import edge stay but no UI, export or write-back path exposes it as a material attribute.

Edits are validated before they become requests. A `name`, `product_type` or `unit` that trims to
nothing is refused - newlines count as blank, and `product_url` is exempt because "no page for this
item" is a real answer - while padding is stored as sent rather than silently rewritten, and every
string value is capped at 1000 characters. The reason a reviewer gave is not a column: `decide()`
records it in the audit event, `ml_change_request_decision_reason()` reads it back for both the
queue and the request detail, and it stays readable after the request row is pruned.

Measured on a fresh PostgreSQL 16 database: `scripts/writeback_contract.sql` green with zero
residual rows, `tests/test_writeback.py` 38/38 with `ML_WRITEBACK_DSN`,
`tests/test_writeback_api.py` 19/19 in the `api` job, and the full suite
`225 passed, 33 skipped` on the CI `api` recipe. The dedicated `writeback` CI job rebuilds this
from scratch on every PR with its own `ml_writeback` database; no `ML_JF_*` credential is set
there.

Design (final): [`docs/t3-writeback.md`](docs/t3-writeback.md). Verification log and decisions:
[`docs/t3-progress.md`](docs/t3-progress.md).

---

## Manual query and bulk edit UI

`GET /ui` serves a no-build browser screen backed by the same API: search, facets, structured
filters, saved views and a read-only result table with paging. It is English-only because
`check_english.py` scans `.html`, `.css` and `.js`. Token and actor are kept in `localStorage` and
sent as headers; the shell itself loads without authentication. The grid stages edits locally and
files them through `POST /v1/change-requests/batch` - every edit becomes a `material_change_request`,
never a direct `UPDATE material`, so the allowlist, base fingerprint, watermark and audit trail still
apply. Staging survives a refresh but not a closed tab (`sessionStorage`). The Review tab shows the
actionable queue, a detail drawer with stale / inactive / own badges, and single or bulk decisions
with a required reason; `decision_reason` is read back from the audit event, not a column. The Add tab
files a proposed new material through `/v1/material-create-requests` with a schema-driven form, a
grouped preview plus collapsed JSON, and in-place editing of a pending request - the material row is
created only after approval by a worker. The Review tab's `creates` chip queues those requests;
its detail shows the proposed row and an event-backed reason with only the `own` badge (a create has
no base to go stale against), and decisions go through the same single-request routes. See
[`docs/ui-bulk-edit.md`](docs/ui-bulk-edit.md) for the 23 decisions and the seven-step delivery plan,
all merged in PRs #22-#27. Five deferrals remain open in section 16 (batch identity, where the reason
rule lives, the cap's home, event history in the grid and timestamp formatting); none blocks the
screen.

### Running the UI locally

Two ways to look at it, depending on what you need:

- **Interactive (real API + database)** - `python scripts/start_ui.py`. The script starts the
  `ml-postgres16` Docker container if needed, creates a `ml_preview` database, loads every schema
  in CI order and imports the Blum and Laminex sample catalogues (all skipped on a later run if the
  data is already there), then serves the real UI at `http://127.0.0.1:8765/ui`. File a create in the
  Add tab, approve it as a different `X-Actor` in Review, then run
  `python scripts/apply_change_requests.py --dsn postgresql://postgres:postgres@127.0.0.1:5432/ml_preview --apply-creates`
  to create the material. Needs Docker, Python and `api/requirements.txt`; options such as
  `--db`, `--port`, `--no-browser` and `--skip-db` are in `scripts/start_ui.py --help`.
- **Static snapshot (no setup)** - double-click `material-library-preview.html`. It inlines the
  stylesheet and a real data snapshot and renders all three tabs with local filtering, form preview
  and the Review queue. Nothing is written and no API is called; it is for looking at the screen,
  not for exercising the approval flow. Rebuild it from a fresh snapshot with
  `python scripts/build_preview.py`, which reads `preview_snapshot.json` and the stylesheet.

Publishing as a shareable link is a decided follow-up, not wired up yet: when asked, publish the
static snapshot as a static site (see the project notes for why the real app is not published).

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
- [x] **Manual query and bulk edit UI**: no-build ES-module screen at `/ui` with facets, structured filters, saved views, a staging grid with schema-driven editors, a review sheet that files batches, and a Review tab with stale / inactive / own badges and single or bulk decisions; 23 decisions and seven merged steps (PRs #22-#27)
- [x] **Approval-based new materials**: `material_create_request` table and submit / update / decide / apply SQL, a `--apply-creates` worker pass and 90-day retention, five HTTP routes, an Add tab with a schema-driven form and grouped preview, and a Review `creates` chip; six merged steps (PRs #29-#34)

**Next step: operational hardening, not another structural gap.** The model layer now covers design, search, supplier imports, duplicate merge, MCP, real-data ETL, approval-based write-back, the manual edit UI and approval-based creation of new materials, and the four T3 consistency follow-ups are merged. `docs/ui-create-material.md` records all six shipped steps (PRs #29-#34): a proposed row is filed from the Add tab, decided by a second actor in Review, and turned into a material by `--apply-creates` - there is deliberately no `POST /v1/materials`. Every state in the five-state machine now has a way out: `--apply` for `approved`, `--retry` for a known failure, and `--verify` for an outcome nobody could see. What remains depends on the live source system: incremental sync needs joineryflow's `updated_at` and real credentials, while `Idempotency-Key` support, the exact workspace guard, the `GET` response shape and a read-scoped token are open questions recorded in [`docs/t3-writeback.md`](docs/t3-writeback.md) §16 and [`docs/t3-progress.md`](docs/t3-progress.md). Request retention is no longer open: the 90-day prune CLI and the watermark are shipped. Who schedules the worker and the prune job is an operations choice. ETIM/eCl@ss codes and EPD collection stay reserved slots until real supplier classification data arrives. T1b closed the two gaps the read API used to advertise: families collapse by default (`expand=variants` returns the records) and `format=json` exports the versioned envelope validated by `schema/export.schema.json`. `docs/t2-merge.md` §9 explains recovery without an `unmerge()`.

---

## License

[MIT](LICENSE) — the design documents, data model, and prototype code are all free to use and modify.

Brand names in the prototype sample data (Blum, Hettich, Laminex, Caesarstone, etc.) are used only to demonstrate field structure and search behaviour; they are unrelated to their respective trademark holders and contain no real prices or commercial terms.
