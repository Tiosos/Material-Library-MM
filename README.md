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
│   └── material.schema.json    UMR exchange-format JSON Schema (contract)
├── api/                        read-only HTTP API (FastAPI, T1)
├── eval/                       golden search cases + deterministic evaluation fixture
├── scripts/eval_search.py      search evaluation runner (MRR@10, hit@1, zero-result, level accuracy)
├── tests/                      HTTP smoke tests (pytest + TestClient)
├── mapping/
│   └── joineryflow.yaml        9 catalog tables → UMR field mapping, write-back, sync spec
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
python -m pytest -q tests                                        # 21 HTTP contract tests
```

Design and decisions: [`docs/api-v1.md`](docs/api-v1.md) (endpoints, SQL additions, instrumentation, evaluation thresholds). Implementation log with verification for every step: [`docs/t1-progress.md`](docs/t1-progress.md).

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
- [ ] MCP server on top of the read API (T1b; the API is the stable layer it would wrap)
- [ ] Real-data ETL (joineryflow database connection or exported snapshot)
- [ ] write-back executor (the request table is ready, the PATCH executor is not done)

**Next step: MCP server on the read API (T1b), then the real-data ETL.** `docs/api-v1.md` §12 lists what the read API deliberately leaves out (`expand=variants` family collapsing, export envelope); `docs/t2-merge.md` §9 explains recovery without an `unmerge()`.

---

## License

[MIT](LICENSE) — the design documents, data model, and prototype code are all free to use and modify.

Brand names in the prototype sample data (Blum, Hettich, Laminex, Caesarstone, etc.) are used only to demonstrate field structure and search behaviour; they are unrelated to their respective trademark holders and contain no real prices or commercial terms.
