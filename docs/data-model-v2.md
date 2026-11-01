# v2 Data Model (Batch B Implementation)

> Basis: your decisions (run internally first, design the contract as if external; one-way + approval-based write-back; same supplier and material at 16/18/25mm form one family but remain 3 records; < 50k rows over three years; **all English in the database**; reserve a slot for EPD).
> Deliverables: `schema/material.sql` (DDL, already run successfully on PostgreSQL 16), `schema/search.sql` (five-level graceful-degradation search), `schema/seed.sql` (category tree/units/11 type profiles), `schema/sample_demo.sql` (samples + acceptance queries), `schema/material.schema.json` (v2 exchange contract).

---

## 1. From Decisions to Design: Three Direct Impacts

| Your decision | Implementation |
|---|---|
| **Same material at 16/18/25mm is one family, but 3 records in the database** | `material_family` only handles belonging and aggregation: one family holds 3 `material` rows, 3 `material_uid`s, 3 SKUs. Family key = brand + series + colour + substrate; thickness/sheet size go into `axis_keys` and `variant_values`. **A family is not a record, it is an index.** |
| **All English** | Drop the multilingual tables; `synonyms` holds English aliases only (black melamine / MDF black). Chinese input is not promised; if it is needed later, it only means putting values into `synonyms`, with no structural change. |
| **Run internally first, design the contract as if external** | The exchange format carries `schema_version`; classification hangs off `material_classification` (slots for ETIM/eCl@ss/UNSPSC/Uniclass are reserved); `additionalProperties:false` keeps strictness. |
| **One-way + approval-based write-back** | `material_change_request` stores per-record diffs (`[{path,before,after}]`), state machine `pending → approved/rejected → applied/failed`; only after approval does the executor PATCH joineryflow. No "two-way real-time sync" code path remains in the repo. |
| **< 50k rows** | PostgreSQL, fixed; the fuzzy tier uses `pg_trgm`'s `word_similarity` — no Meilisearch, no vector database. Every query at the 50k-row scale uses indexes. |
| **Reserve a slot for EPD** | `material_epd` table + the `epd` object in the contract (field names aligned with openEPD / ILCD+EPD for now); not collected yet. |

---

## 2. Table Map

| Table | One-liner | v1 problem |
|---|---|---|
| `material_category` | Category tree (`board.melamine.mdf`) | v1 had 11 flat strings, no hierarchy |
| `material_classification` | Attach ETIM/eCl@ss/UNSPSC/Uniclass | None |
| `party` / `party_alias` | Supplier/brand entities + aliases | v1 was bare text; three spellings of Laminex = three facets |
| `unit` | UCUM code + SI factor | None; `18` and `1.8cm` were not comparable |
| `material_profile` | Which fields each category should have (11 categories already seeded) | v1 had the table but no data; validation was a slogan |
| `material_family` | Product family (grouping index) | None; the 3 records of a family were unrelated |
| `material` | One row = one SKU-level record | Generated-column DDL would not run; `rev` had no history |
| `material_identifier` | Identifier book | Primary key included nullable `source_system`; inserts failed outright |
| `material_measure` | Measures with unit/source/method/validity period | Bare JSON only |
| `material_price` | Price history (supplier entity FK) | Supplier was text |
| `material_relation` | Substitute/accessory/supersede/combination | No evidence or confidence |
| `material_revision` | Full-row snapshot per revision | None |
| `material_event` | Audit stream + webhook outbox | None |
| `material_change_request` | Diff requests for approval-based write-back | None |
| `material_usage` / `search_log` / `search_click` | Usage frequency and search tracking | None ("complaint rate" could not be quantified) |
| `material_duplicate_candidate` | Duplicate candidates generated at import | None |
| `material_epd` | Placeholder for sustainability data | None |

---

## 3. Key Mechanisms

### 3.1 Search Vector: From "Generated Column That Won't Run" to a Trigger

The v1 approach (PG rejects it outright: a generated expression cannot contain a subquery or reference other rows):

```sql
-- v1 (wrong):
search_tsv tsvector GENERATED ALWAYS AS (
  setweight(to_tsvector('simple', (SELECT string_agg(...) FROM material_identifier ...)), 'A') || ...
) STORED
```

v2: `search_tsv` is a plain column; a `BEFORE INSERT/UPDATE` trigger recomputes it with `ml_tsv_from_row(NEW)` (reading NEW directly, not the table, so it never sees stale values); after changes to child tables (identifiers/measures/prices/relations), an `AFTER` trigger refreshes the parent row.

Weights: A identifiers → B name/brand/model/family name → C category/supplier/unit/tab → D description/attributes/measures/synonyms/tags.

### 3.2 Five-Level Graceful Degradation: the Level Is Decided by "Where It Stops"

| Level | Trigger condition | Example (measured) |
|---|---|---|
| `exact` | Whole string equals an identifier | `LAM-MEL-BLK-18` → 1 row |
| `prefix` | A single token ≥ 4 chars hits an identifier prefix | `LAM-MEL` → 3 rows |
| `and` | All terms match | `laminex black` → 3 rows |
| `relaxed` | Any term matches | `laminex black hinge` → 5 rows (3 panels + 1 hardware + 1 Polytec) |
| `fuzzy` | `word_similarity > 0.45` (name/family name/synonyms) | `laminx` → 3 panels, score 0.7143 |

Three bugs in the v1 prototype are fixed:

1. **A single letter no longer reports `exact` falsely**: `b` now returns `none` (v1 returned 27 rows labelled "exact identifier match").
2. **Prefix is its own tier**: v1 labelled prefix results as `and`, so the UI could not explain "why it finds results before I finish typing".
3. **All filters pushed down**: `black` + `colour=Black` gives a total of 4 after filtering, and paging 2 rows does not affect the total (v1 "fetched 40 rows, then filtered in memory", so results vanished as soon as paging changed).

Scores are no longer normalized within a batch (v1 divided by the batch maximum, so top1 was always 1.0); they are now raw `ts_rank_cd` / `word_similarity` values, so thresholds can be set directly and results can be cached.

### 3.3 Units and Measures

```sql
-- Same 18mm board: the supplier catalogue gives 18 mm, the on-site re-measure records 1.8 cm
SELECT m.name, mk.value_num, mk.ucum, mk.value_num * u.si_factor AS metres
  FROM material_measure mk JOIN unit u ON u.code = mk.ucum ...
-- Both rows are 0.018000 m
```

Filtering for "18mm" no longer relies on a hard-coded `thickness_mm` branch; instead `value_num * si_factor` has to fall inside the range — `18mm / 1.8cm / 0.018m` are equivalent.

### 3.4 Sync Cursor: sync_seq instead of updated_at

`material.sync_seq` is monotonic, and any meaningful write (child-table changes included) bumps it. External systems pull increments with `?since=<seq>`, so bulk imports and clock rollbacks never miss a record. Derived-column refreshes (recomputing tsv only) do **not** bump it, so consumers do not fetch for nothing.

### 3.5 Versions and Events

- `material_revision`: every meaningful change writes one full-row snapshot (`to_jsonb(NEW) - 'search_tsv'`), with `rev` monotonically +1.
- `material_event`: `entity + op + payload(before/after)`, both the audit stream and the webhook outbox.
- Measured event distribution: `material/insert 6`, `material_identifier/insert 7`, `material_measure/insert 5`, `material_price/insert 3`.

Note: events must be written in an `AFTER` trigger — during `BEFORE INSERT` the parent row does not exist yet, so writing `material_revision` fails on the foreign key. This is a pitfall we ran into locally; already fixed.

### 3.6 Archive Only, Never Hard-Delete

The `BEFORE DELETE` trigger raises an error unless the session sets `ml.allow_delete=on` (for maintenance). Interception verified in testing.

---

## 4. Acceptance Results (Run on PostgreSQL 16)

```
== A. L0 full identifier ==     LAM-MEL-BLK-18            → exact,  1 row
== B. L1 identifier prefix ==   LAM-MEL                   → prefix, 3 rows
== C. L2 all terms ==           laminex black             → and,    3 rows
== D. L3 relaxed ==             laminex black hinge       → relaxed,5 rows
== E. L4 fuzzy matching ==      laminx                    → fuzzy,  3 rows (0.7143)
== F. Single letter ==          b                         → 0 rows (no false exact)
== G. Filter pushdown ==        black + colour=Black      → total 4 / page 2
== H. Unit conversion ==        18mm and 1.8cm            → both 0.018000 m
== I. Product family ==         3 records → 1 family, thickness 16~25, $38~$58
== J. Facet convergence ==      after black, brand has only Laminex 3 / Polytec 1
== K. API payload ==            material_search_page() returns level/total/results
== L. Completeness ==           required_count / present_count computable
== M. Incremental sync ==       change one price → that row's sync_seq advances
== N. Versions/events ==        rev snapshots 6, events grouped by entity
== O. Approval write-back ==    1 pending diff request
== P. Hard delete ==            intercepted (NOTICE)
```

Reproducible:

```bash
docker run --rm -d --name ml-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=ml postgres:16-alpine
docker cp schema ml-pg:/schema
docker exec ml-pg psql -U postgres -d ml -v ON_ERROR_STOP=1 \
  -f /schema/material.sql -f /schema/search.sql -f /schema/seed.sql -f /schema/sample_demo.sql
```

All three scripts are re-runnable (DDL uses `IF NOT EXISTS` / `CREATE OR REPLACE`, seed uses `ON CONFLICT`).

---

## 5. Relationship to v1

- **No migration script**: v1's DDL never ran successfully (the generated-column statement necessarily errors), so there is no existing database to migrate.
- **The `proto/` prototype stays untouched**: it was a SQLite check of search feel and shares no DDL with the PG model; once the API skeleton is up, the prototype can be retired or turned into "a static page backed by PG".
- **`mapping/joineryflow.yaml` still needs a v2 update**: three additions — ① `category` → `category_code` (9 tables mapped to category-tree nodes); ② generate `family_id` by the family-key rules; ③ move panel thickness/sheet size out of `attributes` into `material_measure`. This item is best done together with data import (B9) so the mapping is not changed twice.

---

## 6. Not Done Yet (Ordered by Your Choices)

| # | Item | Status | Notes |
|---|---|---|---|
| B9 | Import pipeline (Excel/CSV first, PDF second, portal scraping last) | Done for CSV/XLSX | `scripts/import_file.py` + `mappings/<vendor>.yaml`; measured 122 Blum rows → 105 materials, idempotent. PDF extraction covers the Blum hinge section only |
| B7 | Duplicate merge workflow (candidates already persist, merge action missing) | Partial | The candidate table exists; merging needs "which record to keep + field survival rules". This is the next task (T2) |
| B8 | MCP server | Not started | Suggested as a one-day wrap-up once REST is finalized. The read API (T1) is now that REST layer |
| — | Read API v1 (FastAPI over the SQL functions) | Done | `api/` serves 10 read-only endpoints; `schema/api.sql` holds the payload functions; contract tests and a search evaluation gate run in CI (`docs/api-v1.md`, `docs/t1-progress.md`) |
| — | ETIM/eCl@ss real-code mapping | Not started | Tables and fields are reserved, waiting for the first supplier catalogue |
| — | EPD collection | Not started | Table and contract are reserved |

**Suggested next step**: connect the first real data (Excel → UMR), run `material_search` and `v_material_completeness` against a real catalogue, then decide the B7 merge rules and the API shape. The model layer no longer carries debt that would force rework once data volume grows.
