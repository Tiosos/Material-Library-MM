# REST API v1 (T1) — Design

> Status: **proposal, not implemented.**
> Goal: turn the already-verified PostgreSQL layer into something another system can call, and give "forgiving search" a number that CI can defend.
> Related: [`design-v1.md`](design-v1.md) §6 (reference/import), [`data-model-v2.md`](data-model-v2.md) §6 (not done yet), [`review-v1-and-plan-v2.md`](review-v1-and-plan-v2.md) A6/A9.

---

## 0. Scope

| In scope (T1) | Out of scope (later task) |
|---|---|
| Read-only HTTP surface over the existing SQL functions: search, detail, family, facets, suggest, changes, export | Any write: merge (T2), change-request executor (T3), import endpoint (exists as a CLI, fine) |
| `MaterialHit` / `UMR v2` payloads, `schema_version` on every response | Auth beyond a shared token, per-user permissions, multi-tenancy |
| `search_log` / `search_click` instrumentation at the HTTP edge | MCP server (T1b: a one-day wrapper over the same SQL) |
| `eval/golden.yaml` + `scripts/eval_search.py` + CI thresholds | Meilisearch / pgvector / semantic search |
| Fixing three defects in `search.sql` that the API would otherwise advertise | Replacing `proto/` (it stays as the zero-dependency demo) |

**Non-goal, stated explicitly:** the API must not become the place where material logic lives. Every payload is produced by a SQL function; Python only parses parameters, opens a connection, and sets status codes.

---

## 1. Why this is the next step

1. **The engine is done but unreachable.** `material_search_page()` already returns the complete search payload (`level`, `total`, `suggestion`, `results` with family and measures) — see `schema/search.sql:247-286`. Today the only way to reach it is `psql`. An API is a thin shell over a payload that already exists.
2. **The differentiator is unmeasured.** `search_log` (`schema/material.sql:321`) and `search_click` (`schema/material.sql:333`) exist and have never been written to. Without them, "forgiving search" cannot be regression-tested, weights cannot be tuned, and "should we switch to Meilisearch" stays a matter of taste.
3. **There are already two search implementations.** `proto/search.py` is a separate SQLite implementation; the prototype's schema has already drifted from `material.sql` (it flattens `ref_count`/`prices`/`relations`, `docs/review-v1-and-plan-v2.md:210`) and it has no family, measure, or version tables (`docs/data-model-v2.md:146`: the prototype shares no DDL). Every search change must be made twice until the HTTP surface exists; after T1 the prototype can become a thin client of `/v1` (or be retired).

The API is also the cheapest unlock: T2's merge UI, T3's approval screen, and the MCP wrapper all attach to the same SQL functions.

---

## 2. Architecture: SQL owns payloads, HTTP owns transport

```
client ──HTTP──▶ api/ (FastAPI, ~350 lines) ──▶ material_search_page()  ─┐
                        │                     material_detail()          │  schema/*.sql
                        │                     material_facets()          │  (jsonb payloads,
                        │                     material_changes()         │   STABLE, no writes)
                        │                     material_export()          ─┘
                        └──▶ INSERT search_log / search_click  (edge only)
```

Three rules, so the seam stays one line thick:

1. **No material logic in Python.** If it can be answered from the database, it is a SQL function. Payload shape lives in `schema/`, where `psql` can assert it without starting a server.
2. **The API is replaceable.** The MCP server and any future UI call the same functions; FastAPI is transport, not architecture.
3. **Logging is an edge concern.** `material_search*` are `STABLE` (`schema/search.sql:30`), so they cannot write anyway. Keeping them pure means the evaluation harness can run them thousands of times without polluting analytics.

Consequence: almost the whole test suite is `psql` assertions (as today, `.github/workflows/ci.yml:74-125`), and the HTTP layer gets a small smoke test. That keeps CI fast and the new dependency surface minimal.

---

## 3. Endpoints

Base path `/v1`. JSON in, JSON out, UTF-8, no envelope wrapping beyond the fields below.

| Method | Path | Backing SQL | Notes |
|---|---|---|---|
| GET | `/healthz` | `SELECT 1` (1s timeout) | 200 / 503 only; never 500 |
| GET | `/v1/meta` | `count(*)` on `material`, `max(sync_seq)`, schema version | Lets a client detect a re-seeded database |
| GET | `/v1/materials` | `material_search_page()` | Search **and** browse: omit `q` for `level=browse` |
| GET | `/v1/materials/{uid}` | `material_detail()` (new) | Full UMR v2 document; 404 if unknown |
| GET | `/v1/materials/{uid}/family` | `v_family` + members | Collapsed family row + its variant records |
| GET | `/v1/facets` | `material_facets()` | Same filters as `/v1/materials`; counts only over the current hit set |
| GET | `/v1/suggest` | `material_suggest()` | Autocomplete / "did you mean" |
| GET | `/v1/changes` | `material_changes()` (new) | `?since=<sync_seq>`; the joineryflow sync feed |
| GET | `/v1/export` | `material_export()` (new) | `format=jsonl\|csv`, streamed, full UMR v2 |
| POST | `/v1/searches/{log_id}/clicks` | `INSERT INTO search_click` | `{material_uid, rank}`; idempotent per `(log_id, uid)` |

### 3.1 Search parameters

| Param | Type | Maps to | Notes |
|---|---|---|---|
| `q` | string, ≤ 200 chars | `p_q` | Empty/absent → browse |
| `category` | string | `p_category` | Category **code**, exact |
| `brand`, `supplier` | string | `p_brand`, `p_supplier` | Substring (ILIKE), because "Laminex Australia" is a party alias |
| `attrs` | JSON object | `p_attrs` | JSONB containment (`@>`), e.g. `{"colour":"Black"}` |
| `measures` | JSON object | new | Unit-aware numeric ranges, see §4.4 |
| `limit` | int 1..100, default 20 | `p_limit` | |
| `cursor` | opaque string | `p_offset` | `base64("o:<n>")` |
| `expand` | `variants` | — | Accepted but not implemented in v1: family collapsing changes ranking and paging, so it is deferred to T1b (decision D6). Today one hit per material record is returned, `expand=variants` and no `expand` give the same result. |

`attrs` is parsed as JSON at the edge and must be an object; invalid JSON or an array is `400`, never a `500` from `::jsonb` casting inside the function.

### 3.2 Search response

```json
{
  "schema_version": "2",
  "q": "blum hinge 450",
  "level": "relaxed",
  "total": 3,
  "suggestion": null,
  "took_ms": 7,
  "next_cursor": "bzoyMA==",
  "results": [
    {
      "material_uid": "mat_01K9XFT7GJSXCA6JANCTXV3XH0",
      "name": "Blumotion Soft Close Hinge",
      "category": "hardware.hinge",
      "brand": "Blum",
      "supplier": "Blum",
      "family_id": "fam_01K9XFT7GJSXCA6JANCTXV3XH0",
      "family_name": "Blum CLIP top BLUMOTION Overlay NI",
      "variant_values": {"boss": "INSERTA"},
      "unit": "ea",
      "unit_cost": 6.42,
      "score": 7.4121
    }
  ]
}
```

- `level` is the tier that actually matched: `exact | prefix | and | relaxed | fuzzy | browse | none`. It is part of the contract, not a debug aid: a client may show "showing approximate matches".
- `total` is the number of matches **before** paging (see §4.2 — today it silently caps at 200).
- `suggestion` is non-null only when `total = 0`, and only when a name, brand or family name is at least 0.3 similar (`material_suggest`, trigram `%`). In practice the fuzzy tier already catches near-misses, so a zero-result query usually has no candidate either: on the sample corpus `q=laminx` is `fuzzy` (suggestion `null`), `q=caesarstone bench` is `none` (suggestion `null`). The spelling surface that always works is `GET /v1/suggest`, which uses the same candidates without the "no hits" precondition (`?q=laminx` -> `Laminex`, similarity 0.5). §11 criterion 1 was corrected to test both endpoints instead of inventing a query.
- `score` is monotonic **within one query** and is not normalised per batch: exact hits score `1.0`, prefix `0.9`, and the text levels use `ts_rank_cd` / trigram similarity ordered by `score DESC, coalesce(u.ref_count,0) DESC` (`schema/search.sql:89-169`). It is a ranking signal, not a probability — do not threshold on it across levels or across queries.
- Hit objects are `MaterialHit` (the fields above), **not** full UMR records: a `score` field would violate the UMR contract's `additionalProperties: false` (`schema/material.schema.json:8`). Full records come from `/v1/materials/{uid}` or `/v1/export`.

### 3.3 Errors

Problem-shaped, one shape only:

```json
{"error": {"code": "not_found", "message": "material_uid not found", "detail": {"uid": "mat_..."}}}
```

`400 bad_request` (bad `attrs`/`measures`/`limit`), `401 unauthorized` (token configured and missing/wrong), `404 not_found`, `503 unavailable` (database down or statement timeout). Unexpected exceptions return `500` with `code: internal` and log a stack trace server-side only — no SQL text in responses.

### 3.4 Semantics worth fixing in v1, not later

- Collapsing a family to one hit is the right default for a shopping surface (`docs/data-model-v2.md:12`: `laminex black` matches three rows that differ only in thickness), but it is a ranking and paging change inside SQL, not an edge feature: hits are per material record, and collapsing after paging would produce short pages with wrong cursors. **Decision D6: v1 accepts `expand=variants` for forward compatibility and returns one hit per material record; family collapsing lands in T1b together with a family-aware search function.** `/v1/materials/{uid}/family` already returns the members of one family, so a UI can group today.
- `/v1/changes` is what makes the library usable *upstream* of joineryflow: `sync_seq` is monotonic and bumped by child-table triggers (`schema/material.sql:162`), unlike `updated_at`.
- `superseded_by` (`schema/material.sql:116`) must be honoured. **This is a fix, not an existing behaviour:** `material_search` today excludes only `archived` (`schema/search.sql:53-55`), so after the first merge the dead record keeps appearing in results. v1 excludes `superseded` from search while keeping it resolvable by UID (200 with `status: superseded`), so a stale client UID degrades into a redirect instead of a 404.

---

## 4. SQL additions (the only schema work in T1)

All additions live in `schema/search.sql` (search-adjacent) or a new `schema/api.sql`; they are `STABLE`/`sql` functions so psql can assert them.

### 4.1 `material_detail(p_uid text) RETURNS jsonb`

The single UMR v2 document: base row from `v_material` (`schema/material.sql:542`) plus `identifiers`, `measures`, `prices`, `relations`, `classifications`, `epd`, `family`. Rules:

- Preferred measure only (`is_preferred`), full measure rows only when `?measures=all`.
- Price history capped at the last 12 rows, newest first.
- Relations expanded to `[{uid, name, kind, note}]` so a client can render substitutes without a second call.
- Returns `NULL` for unknown UID (the API maps that to 404) rather than raising.
- **Acceptance:** every `GET /v1/materials/{uid}` payload validates against `schema/material.schema.json` (pytest + `jsonschema`, looped over all seeded rows); this is the contract test, not a hand-written field list.

### 4.2 Pagination and a truthful `total` in `material_search`

Current state: `v_limit := least(greatest(...), 200)` (`schema/search.sql:33`) and `material_search_page` computes `total` as `count(*)` over those hits (`schema/search.sql:268`). So `total` and `material_facets` both cap at 200 — a lie the API would publish. Changes:

- `material_search(..., p_limit integer DEFAULT 40, p_offset integer DEFAULT 0)`; clamp raised to 5000; `OFFSET` appended to the dynamic SQL.
- Add an OUT column `total bigint`, computed as `count(*) OVER ()` in each level's SELECT list. Window functions run before `LIMIT`, so this is the true match count; adding an OUT column does not break existing callers that select named columns (`material_facets`, `material_search_page`, CI assertions).
- `material_search_page` returns `total` from the first hit row (0 when empty) and applies `p_offset`.
- `material_facets` keeps asking for 5000, which now actually works.
- Same pass, one WHERE change: exclude `status = 'superseded'` from all search levels (today only `archived`, `schema/search.sql:53-55`). `material_detail` still resolves superseded UIDs, so links do not break.

**Cursor is offset-based in v1.** Keyset pagination over `(score, material_uid)` is the better end state, but the score is produced per query by five different branches; it is a real refactor. Offsets are correct at this scale (< 50k rows) and the cursor is opaque, so switching the encoding later is not a breaking change. Threshold to revisit: 10k rows, or the first duplicate-across-pages report.

### 4.3 `material_changes(p_since bigint, p_limit integer DEFAULT 500) RETURNS TABLE(...)`

`v_material` rows with `sync_seq > p_since`, ordered by `sync_seq`, plus the derived `total_available` and `next_since`. Deleted/merged records never disappear silently: T2 will emit a tombstone event, and this feed already carries `status`/`superseded_by`, so a consumer learns about it.

### 4.4 `p_measures`-style unit-aware filtering

The promise "type the spec, not the name" is only true for `attrs` equality today: `thickness:18` works in the SQLite prototype and in the `sample_demo.sql` H query (`schema/sample_demo.sql:151`), but **`material_search` has no unit-aware path** — `schema/search.sql:65-67` is plain `@>` containment. Without this, `/v1/materials?measures=` would be a fourth-level feature that only exists in the demo.

Target parameter shape:

```json
{"thickness_mm": {"gte": 17.5, "lte": 18.5, "unit": "mm"}}
```

Generated predicate (one `EXISTS` per key, so multiple ranges AND together):

```sql
AND EXISTS (
  SELECT 1 FROM material_measure mk JOIN unit mu ON mu.code = mk.ucum
   WHERE mk.material_uid = m.material_uid
     AND mk.key = %L
     AND mu.dimension = %L                       -- reject mm-vs-kg before comparing
     AND mk.value_num * mu.si_factor
         BETWEEN %s * (SELECT si_factor FROM unit WHERE code = %L)
             AND %s * (SELECT si_factor FROM unit WHERE code = %L)
)
```

Comparison happens in SI units via `unit.si_factor` (`schema/material.sql:69`), so `18mm`, `1.8cm`, and `0.018m` are the same query. `gte`-only / `lte`-only are allowed; a mismatched dimension is `400`, not an empty result. Know the constraint: `material_measure` and `attributes` are written from different columns — the pipeline writes a measure for `target: measure` fields and an attribute for `target: attribute` / `extra_headers` columns (`docs/import-pipeline-b9.md:78`), and `mappings/laminex.yaml` maps `thickness` as a measure **only**. So a thickness filter matches Laminex rows, but a fact that only ever landed in `attributes` will not; `v_material_completeness` (`schema/material.sql:592`) is where the gap shows up, and the mapping file is where it is fixed.

---

## 5. Instrumentation

`search_log` and `search_click` exist; nothing writes them. T1 wires them at the edge, where `took_ms` and `actor` actually exist.

| Field | Source |
|---|---|
| `q` | verbatim, trimmed, truncated to 200 chars (never normalised: the raw string is the evaluation input) |
| `level` | from the payload |
| `total` | from the payload |
| `filters` | `{"category","brand","supplier","attrs","measures"}` as submitted |
| `took_ms` | wall clock around the SQL call |
| `actor` | `X-Actor` header, default `anonymous` |

Rules:

- **Never logged:** `/v1/suggest`, `/healthz`, `/v1/meta`, and any request carrying `X-No-Log: 1`. The evaluation harness calls SQL directly, so it never appears here.
- **Click:** `POST /v1/searches/{log_id}/clicks` with `{material_uid, rank}`; the FK to `search_log` (`schema/material.sql:334`) rejects invented ids; `ON CONFLICT DO NOTHING` makes double-clicks harmless. `rank` is the 1-based position the client saw, so a future MRR can be computed from real traffic.
- **Privacy:** no IP, no user agent, no body capture. `actor` is a system name (`joineryflow-web`, `mcp`), not a person.
- **Retention:** 90 days. A maintenance statement (`DELETE FROM search_log WHERE at < now() - interval '90 days'`) is documented, not scheduled in T1; volume at this stage is thousands of rows, not millions.

---

## 6. Evaluation harness

The point of T1 is that "better search" becomes a number in CI.

### 6.1 `eval/golden.yaml`

One file holds both datasets, so the thresholds and the case format cannot drift apart. Shape as implemented:

```yaml
version: 1
thresholds:
  mrr_at_10: 0.75
  hit_at_1: 0.60
  zero_result_max: 0.10
  level_accuracy: 1.0
datasets:
  fixture:
    cases:
      - id: fx-and-brand-colour
        split: tuning
        q: laminex black
        expect_level: and
        expect_min_hits: 3
        expect_any: [mat_FX000000000000000000BRD016, mat_FX000000000000000000BRD025]
      - id: fx-measure-cm-window
        split: holdout
        measures:
          thickness_mm: {gte: 1.75, lte: 1.85, unit: cm}
        expect_level: browse
        expect_total: 3
      - id: fx-none-wrong-dimension
        split: holdout
        measures:
          thickness_mm: {gte: 17.5, lte: 18.5, unit: kg}
        expect_level: none
        expect_total: 0
  blum:
    cases:
      - id: bl-exact-part-71B3590
        split: holdout
        q: 71B3590
        expect_level: exact
        expect_total: 1
        expect_top1: mat_0GT4GJ6SKMBAS3RZJ0PQ74T01P
```

A case may assert any of `expect_level` (always), `expect_total`, `expect_min_hits`, `expect_any`, `expect_top1`, and may carry filters: `q`, `category`, `brand`, `supplier`, `attrs`, `measures`. `expect_level` is the tier that must answer (`exact`, `prefix`, `and`, `relaxed`, `fuzzy`, `browse`, `none`); a case answered by a weaker tier than intended fails even when the right row is found, because a fallback to `fuzzy` is a worse result that MRR alone does not reveal. `expect_any` lists every uid that counts as relevant (MRR uses the first one in the page); `expect_top1` is only used where the ordering is a contract.

- `fixture` runs against `eval/fixture.sql` (39 deterministic rows: all five levels, thickness in mm and in cm, angle in deg, two count units, one family with live, archived and superseded variants, 22 filler rows), so cases do not break when the demo data changes. 18 cases, 9 of them holdout.
- `blum` runs against the two sample imports already in CI (105 Blum hinges, 4 Laminex boards, 109 rows) with real part numbers: 14 cases, 9 of them holdout. Both datasets are evaluated against separate databases, never the same one, so the fixture's names cannot satisfy a Blum case.
- Negative cases (`expect_total: 0`) are kept in the same list as positive ones; the runner excludes them from the zero-result ratio, so "nothing found" is only a failure where something was expected.

### 6.2 Metrics and thresholds

| Metric | Definition | Initial threshold |
|---|---|---|
| MRR@10 | mean of `1/rank` of the first expected uid, capped at 10 | ≥ 0.75 (holdout) |
| hit@1 | share of cases whose expected uid is top-1 | ≥ 0.60 |
| zero-result rate | share of cases with `total = 0` (negatives excluded) | ≤ 10% |
| level accuracy | share of cases whose actual level equals `expect_level` | 100% |

Print the full table (per-case, with `level`, `total`, rank) so a failure is diagnosable without a debugger. `scripts/eval_search.py --dsn $ML_DSN --dataset fixture --json out.json`; exit 1 below threshold. No pytest dependency: it is a script, like `scripts/import_file.py`.

### 6.3 Adding a case

Add a case id, a split, the query or filters, and the expected level; add `expect_any` with the uids that count as relevant when rank matters. If the expected level is arguable, the level logic is the bug: write the case anyway and let CI argue. A holdout case may not be edited to make a run green - if it fails, the search changed and that is the finding; a tuning case may be rewritten when the tiering itself is deliberately changed.

## 7. Contract and versioning

- Every response carries `schema_version: "2"` at the envelope level; export records validate individually against `schema/material.schema.json` (`const "2"`, `required: [schema_version, material_uid, category_code, name]`).
- **Decision D7: the export is a stream of whole UMR documents, not an envelope.** JSONL is one UMR v2 document per line; CSV has the fixed `api.main.EXPORT_COLUMNS` header and JSON-encodes nested values. Version and freshness travel in response headers (`X-Schema-Version: 2`, `X-Exported-At`, `X-Record-Count`), which answers the review finding that recipients cannot tell which version they hold (`docs/review-v1-and-plan-v2.md:213`) while keeping every line independently validatable. No `schema/export.schema.json` is needed, and a line-by-line consumer (the usual tool for JSONL) works without parsing a wrapper.
- **`proto/data/materials.jsonl` is v1-shaped** (it has `category`, `default_supplier`, and no `schema_version`) and therefore does not validate against the v2 contract. T1 must either regenerate it from `/v1/export` or drop it from the README as a "sample"; a contract nobody validates is not a contract.
- Policy: additive fields only within v2; removal/rename requires `/v2` and a deprecation window of one minor release.
- CSV export: fixed header, RFC 4180 quoting, ISO-8601 dates, UTF-8 without BOM.

---

## 8. Runtime and configuration

| Env | Default | Meaning |
|---|---|---|
| `ML_DSN` | none (required) | `postgresql://...` |
| `ML_API_HOST` | `127.0.0.1` | Bind address; loopback by default |
| `ML_API_PORT` | `8848` | Same port the prototype uses, so muscle memory transfers |
| `ML_API_TOKEN` | empty | If set, require `Authorization: Bearer`; empty means no auth |
| `ML_ACTOR_DEFAULT` | `anonymous` | Fallback for `X-Actor` |
| `ML_LOG_SEARCH` | `1` | Kill switch for instrumentation |

- Dependencies (`api/requirements.txt`): `fastapi`, `uvicorn[standard]`, `psycopg[binary,pool]`, `pyyaml`. Pin to minor versions; Python 3.12+ (CI) and 3.14 (local) must both work — if the `psycopg[binary]` wheel is missing for 3.14, the pure-Python `psycopg` plus system `libpq` is the fallback.
- Pool: 1–8 connections, `statement_timeout = 3s`, `application_name = material-library-api`.
- Startup does not run DDL and does not seed. Schema application stays `psql -f schema/...`, exactly as today.
- **Auth is deliberately out of scope for v1**: bind to loopback, document that exposure beyond localhost requires `ML_API_TOKEN` plus a reverse proxy. Revisit trigger: the first non-local consumer.

---

## 9. CI plan

Add one job (`api`) to `.github/workflows/ci.yml`, reusing the existing `postgres:16` service (`.github/workflows/ci.yml:34`):

1. `pip install -r api/requirements.txt`
2. `psql -f schema/material.sql -f schema/search.sql -f schema/seed.sql -f eval/fixture.sql`
3. **psql contract assertions** (the bulk): `material_detail` returns all required top-level keys and its `measures`/`prices`/`relations` counts match the source tables; `total` is exact past 200 rows; `p_measures` returns the same rows for `18mm` and `1.8cm`; `p_offset` pages do not overlap; `material_changes(since)` is ordered and complete.
4. **HTTP smoke test** (pytest + `TestClient`, ~10 tests): status codes, UMR JSON Schema validation for every seeded UID, `attrs` 400, unknown uid 404, `next_cursor` round-trip, one `search_log` row per search, click FK rejection.
5. **Evaluation**: `python scripts/eval_search.py --dataset fixture` (thresholds) and, after the existing Blum import step, `--dataset blum`.
6. Existing psql assertions stay untouched; if §4.2 changes break one, that is a signal, not noise.

---

## 10. Deliverables and effort

| # | File | What | Est. |
|---|---|---|---|
| 1 | `schema/search.sql` (edit) | `total`/`offset`, clamp 5000, `p_measures`, `material_detail`, `material_changes`, `material_export` | 1.0 d |
| 2 | `api/main.py`, `api/db.py`, `api/requirements.txt` | FastAPI app, pool, 10 routes, error mapping | 0.5 d |
| 3 | `api/main.py` (edge) | `search_log` / `search_click` writes, `X-No-Log` | 0.25 d |
| 4 | `eval/fixture.sql`, `eval/golden.yaml`, `scripts/eval_search.py` | Deterministic dataset, 30 cases, metrics, exit codes | 0.75 d |
| 5 | `.github/workflows/ci.yml`, `scripts/api_contract.sql` | New job `api`, SQL contract assertions | 0.25 d |
| 6 | `README.md`, `docs/data-model-v2.md:151-161`, this file | Status table, API section, remove "blocked on six questions" | 0.25 d |

**Total ≈ 3 days.** The only externally visible change to existing behaviour is §4.2 (`total` becomes truthful, facet counts stop truncating at 200); everything else is additive.

---

## 11. Acceptance criteria

1. `uvicorn api.main:app` against a fresh database: `GET /v1/materials?q=laminx` returns `level: "fuzzy"` with `suggestion: null` (suggestions are only for zero-result queries), `GET /v1/materials?q=caesarstone%20bench` returns `level: "none"`, `total: 0, suggestion: null`, and `GET /v1/suggest?q=laminx` returns `Laminex` with similarity 0.5. Gibberish gets no invented nearest neighbour; the spelling hint lives on its own endpoint.
2. `GET /v1/materials/{uid}` returns a document that validates against `schema/material.schema.json` for **every** material row in the seeded demo database — asserted by a loop in the test, not by eye.
3. `GET /v1/materials?measures={"thickness_mm":{"gte":17.5,"lte":18.5,"unit":"mm"}}` and the same request in `cm` return identical UIDs.
4. With 250 seeded rows, `total` reports 250 while `limit=20` returns 20; `next_cursor` reaches the last page exactly once (no gaps, no repeats).
5. Three searches produce three `search_log` rows with non-null `level` and `took_ms`; a click on a bogus `log_id` is rejected by the FK, not silently accepted.
6. `python scripts/eval_search.py --dataset fixture` prints the four metrics and exits 0; lowering a threshold in the YAML (temporarily) makes it exit 1.
7. CI is green, including the pre-existing search-level assertions, import idempotency, and the English-only check.

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| §4.2 touches functions CI already asserts | Medium | Additive OUT column; the clamp change is the only behavioural edit; assertions use < 200 rows |
| Python 3.14 vs `psycopg[binary]` wheels | Low | Fallback to pure-Python `psycopg` + system libpq; CI pins 3.12 |
| Evaluation overfits the fixture | High (false confidence) | `holdout` split in YAML + the `blum` dataset on real catalogue rows |
| API drifts from `proto/` | Medium | README declares `/v1` authoritative; prototype banner; T1b converts or retires `proto/serve.py` |
| Offset cursor duplicates rows under concurrent writes | Low at this scale | Opaque cursor, keyset documented as the 10k-row follow-up |
| Logging volume | Low | 90-day cleanup statement, one index on `at` only |
| `material_suggest` almost never fires inside the search payload | Low | Fuzzy tier covers the same ground; `GET /v1/suggest` is the UI path. Revisit the 0.3 threshold in T1b if real queries show missed corrections |
| Family collapsing (`expand=variants`) deferred | Medium | Clients group via `/v1/materials/{uid}/family`; a family-aware search function is a ranking change and belongs in T1b (D6) |

---

## 13. Open decisions

| # | Decision | Recommendation |
|---|---|---|
| D1 | Is `total` computed exactly (extra scan of the level's WHERE) or exposed as `total_capped: 200`? | Exact. A paging API that reports a capped total is a bug report waiting to happen. |
| D2 | Actor identity: `X-Actor` header, or JWT subject once auth exists? | `X-Actor` now; if tokens arrive later, `sub` becomes the fallback. |
| D3 | Is `measures` (unit-aware spec search) part of v1, or a v1.1 patch? | Part of v1. Without it the API cannot deliver the "type the spec" promise, and the parameter shape is public once shipped. |
| D4 | Does T1 also ship the MCP server? | No. One day after the REST surface is frozen (T1b), reusing the same SQL. |
