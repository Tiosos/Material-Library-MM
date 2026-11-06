# Real-data ETL: joineryflow catalogue -> UMR v2

Status: design. No code for this task is written yet; the numbered steps at the end are the
implementation checklist. The first real dataset available in this environment is the joineryflow
catalogue (`D:\new system test`, the `Tiosos/joineryflow` checkout, workspace `hartwood-joinery`).

What it is: a two-part pipeline. An **exporter** reads the nine joineryflow catalogue tables into a
committed JSONL snapshot; an **importer** turns that snapshot into UMR v2 rows. The split is the
point: the importer never talks to joineryflow, so CI can run the whole pipeline from the snapshot,
and a real export can be refreshed without touching the loader.

## 1. Why this is the next step

Everything downstream already exists and is waiting for data: the read API (`api/`, 10 routes), the
MCP server (`mcp_server/`, seven tools), the duplicate merge (`schema/merge.sql`), and the search
evaluation gate (`eval/golden.yaml`). All of it has only ever run on 109 synthetic or
supplier-catalogue rows. The open question in `docs/data-model-v2.md` 6 and `README.md` is the same
one: does the model survive contact with a real catalogue?

The measured answer is small but real: **18 live rows** across six of the nine tables.

| Table | Rows | Live (not archived) | Notes |
| --- | --- | --- | --- |
| `board_materials` | 20 | 10 | 10 rows are archived `E2E-*` automation leftovers |
| `hardware_materials` | 4 | 4 | Hettich slide, Blum hinge, brass pull, soft-close damper |
| `benchtop_materials` | 1 | 1 | Caesarstone 6131 Bianco Drift 20 mm, CDK Stone, 1450.00 |
| `custom_made` | 1 | 1 | Bespoke signage box, Metalform, 1250.00 |
| `appliances` | 1 | 1 | Miele 60 cm Oven H7164BP, 3450.00 |
| `equipment_hire` | 1 | 1 | Kennards scissor lift, 285.00, rate unit DAY |
| `glass_materials` | 0 | 0 | |
| `timber_materials` | 0 | 0 | |
| `edging_materials` | 0 | 0 | |

The dataset is sparse in a way that matters: **the board rows carry thickness only in the
description** (`18mm White MDF`), not in `thickness_mm`; `colour`, `substrate`, `finish` and
`supplier` are null on every board; `unit_cost` is filled but `um` is not. A loader that maps
columns one-to-one would produce 18 records that cannot be filtered by thickness and have no
families to collapse. Section 8 deals with that directly instead of pretending the columns are
populated.

## 2. What exists today

| Piece | Where | State |
| --- | --- | --- |
| Mapping spec v1 | `mapping/joineryflow.yaml` (247 lines) | Written from the upstream ORM/migrations; nine tables, common columns, per-table attributes, write-back, sync, export. It has never been executed |
| Supplier import pipeline | `ingest/importer.py`, `ingest/mapping.py`, `scripts/import_file.py` | CSV/XLSX only. Reads headers, resolves aliases, builds `Draft`s, applies them. Idempotent via the first identifier |
| Mapping configs | `mappings/laminex.yaml`, `mappings/blum.yaml` | The free-form-header shape that the supplier importer consumes |
| Schema | `schema/material.sql` (602 lines), `schema/search.sql`, `schema/api.sql`, `schema/merge.sql` | Complete for v2; `material.source_payload`, `material.source_id` and `material_identifier` are unused so far |
| Snapshot of the source | none | This document defines it |

`docs/data-model-v2.md:147` lists three things v1's mapping is missing and says they are best done
together with the data import: `category -> category_code`, `family_id` generation, and moving
panel thickness/sheet size out of `attributes` into `material_measure`. This task is that moment.

## 3. Principles

1. **The snapshot is the contract.** The importer reads one file and nothing else. A change in
   joineryflow's columns shows up as a change in the snapshot, which is committed and reviewable.
2. **Deterministic output.** The same source rows produce byte-identical UMR rows, and re-running
   the import changes nothing. Uids come from a stable hash of `(source_system, table, primary key)`.
3. **One write path.** All writes go through `ingest/importer.py`'s `apply()`, so triggers
   (`material_revision`, `material_event`, `sync_seq`) remain the only place that records history.
4. **Explicit is better than clever.** Derived values (thickness parsed from a name) are marked
   with `source_ref` and emit a warning, so a human can audit every guess.
5. **Never invent commercial data.** A missing price stays missing; `unit_cost` is a cache of the
   newest `material_price` row, not a second source of truth.
6. **One workspace per database.** joineryflow isolates catalogues by workspace; UMR v2 has no
   namespace column, so the importer refuses a snapshot whose workspace differs from the one it was
   told to expect.
7. **The source database is read-only.** The exporter runs in a `READ ONLY` transaction and never
   writes to joineryflow.

## 4. Decisions

| # | Decision | Rationale |
| --- | --- | --- |
| E1 | Snapshot-first: exporter + committed JSONL fixture; the importer never connects to joineryflow | CI has no joineryflow database; a committed snapshot makes the import reproducible and reviewable. Confirmed by the project owner |
| E2 | All nine tables are mapped; archived rows are skipped by default, `--include-archived` includes them as `status='archived'` | Archiving is a business action in joineryflow, not master data; this also filters the `E2E-*` leftovers. Confirmed |
| E3 | Dimensional columns go to `material_measure` (ucum `mm`/`m`), everything else to `attributes` | Makes the T1 unit-aware filter, the T2 scan and the family axis work on real data. Confirmed |
| E4 | Families are generated from a per-table key and axis; a key with every part null falls back to the description with its leading size token removed | Sparse rows would otherwise collapse unrelated boards into one family. Confirmed (generated, per-table) |
| E5 | Identity is `(legacy_id, "<table>:<primary key>", "joineryflow")`; `sku`/`code` become `supplier_code`/`internal_ref` only when unique across the snapshot | `material_identifier`'s primary key is `(kind, value, source_system)`; two tables sharing a code would silently collide |
| E6 | `material_uid = gen_uid("mat", "joineryflow:<table>:<pk>")` | Re-imports and machine changes never mint a new uid; the same rule the supplier importer uses |
| E7 | Price rows use `valid_from = date(updated_at)` of the source row, `source_system='joineryflow'` | Stable across re-imports (a `date.today()` default would add a row per day) |
| E8 | `archived_at` -> `archived`; `is_active = false` without `archived_at` -> `draft`, on the five tables that have the column | Both are "not for sale now", but only one is a business archive; `draft` keeps it visible to reviewers. `is_active` exists only on `board_materials`, `edging_materials`, `glass_materials`, `timber_materials`, `hardware_materials` |
| E9 | Provenance: `source_system='joineryflow'`, `source_id='<table>:<pk>'`, `source_payload` = the row minus `image_blob` | `source_payload` has been reserved since v2 and is the audit trail for "where did this value come from" |
| E10 | No images in v1 (`image_blob` is a pointer; fetching needs the joineryflow API) | Keeps the exporter to one database connection |
| E11 | `cv_material_mapping` codes are exported as a separate section and imported as `identifier(kind='cv_code', source_system='cv')` | Two rows exist today; they are the only supplier/CV cross-reference joineryflow has |

## 5. The snapshot format

JSONL, UTF-8, LF, sorted object keys, one JSON value per line. First line is the header, every
following line is one source row:

```json
{"kind": "header", "schema_version": "1", "exported_at": "2026-11-03T09:49:53Z",
 "source_system": "joineryflow", "workspace_slug": "hartwood-joinery",
 "workspace_name": "Hartwood Joinery", "include_archived": false,
 "tables": ["board_materials", "edging_materials", "glass_materials", "timber_materials",
            "benchtop_materials", "hardware_materials", "appliances", "custom_made",
            "equipment_hire"]}
{"kind": "row", "table": "board_materials", "source_id": 1, "archived": false,
 "row": {"archived_at": null, "code": "BM-001", "cost_per_sheet": "45.00",
         "created_at": "2026-10-10T11:19:02Z", "description": "18mm White MDF", "...": "..."}}
{"kind": "cv_mapping", "table": "cv_material_mapping", "source_id": 1,
 "row": {"cv_code": "18-PB", "target_material_table": "board_materials",
         "target_material_id": 1, "notes": null}}
```

Rules:

* `row` carries every column of the table **except** `image_blob` (a bigint pointer, not data).
* Numerics are strings (`"45.00"`) to avoid float drift in JSON; `null` stays `null`.
* Timestamps are ISO-8601 UTC with `Z`; `date` columns stay `YYYY-MM-DD`.
* `text[]` columns are JSON arrays of strings.
* Row order is deterministic: table order as listed in the header, then `source_id` ascending.
* A row whose primary key is null or duplicated is an exporter failure, not a skipped line.

## 6. The exporter

`scripts/export_joineryflow.py`:

```bash
python scripts/export_joineryflow.py \
  --dsn postgresql://jf:jf@127.0.0.1:5432/joineryflow \
  --workspace hartwood-joinery \
  --out fixtures/joineryflow-hartwood.jsonl
```

* `--dsn` defaults to `$JOINERYFLOW_DSN`; `--workspace` accepts the slug or the numeric id and
  defaults to `$JOINERYFLOW_WORKSPACE`.
* Reads with `SELECT ... FROM <table> WHERE workspace_id = %s ORDER BY <pk>` inside
  `BEGIN READ ONLY`. `equipment_hire` is filtered the same way (it is workspace-scoped too).
* `--include-archived` adds archived rows to the file and flips the header flag; default is
  `WHERE archived_at IS NULL`.
* `--quiet` prints nothing on success; `--json` writes a report (`{tables: {name: rows}, archived_skipped, bytes}`).
* Exit codes: `0` success, `1` database or shape error (with the table and column named), `2` usage
  (unknown workspace, no rows).
* It refuses to run against a database whose `workspace` table does not contain the slug, and it
  refuses to write outside `fixtures/` unless `--force` is given, so a stray run cannot dump the
  catalogue into a tracked source file.

The first committed snapshot is `fixtures/joineryflow-hartwood.jsonl`: 18 rows (10 board, 4
hardware, 1 benchtop, 1 custom, 1 appliance, 1 hire) plus 2 `cv_mapping` lines.

## 7. Mapping v2

`mapping/joineryflow.yaml` is bumped to `version: 2`. The existing sections (write-back, sync,
export) stay: they describe the same upstream contract. The new sections are the executable ones:

```yaml
version: 2
source:
  system: joineryflow
  snapshot: fixtures/joineryflow-hartwood.jsonl
  workspace: hartwood-joinery

tables:
  board_materials:
    category_code: board
    pk: material_id
    unit: sheet
    own_cost_column: cost_per_sheet
    family:
      key: [brand, colour, substrate, finish]
      axis: [thickness_mm]
      name: "{brand} {colour} {substrate} {finish}"
    measures:
      thickness_mm:  { column: thickness_mm, unit: mm, derive_from_name: true }
      sheet_len_mm:  { column: sheet_len_mm, unit: mm }
      sheet_wid_mm:  { column: sheet_wid_mm, unit: mm }
    attributes: [colour, substrate, finish, sided, grain, rating, hmr, grain_locked, notes]
    identifiers:
      - { kind: supplier_code, column: sku }
      - { kind: internal_ref, column: code }
```

Per table, the same shape:

| Table | category_code | unit | own cost column | family key | family axis | measures |
| --- | --- | --- | --- | --- | --- | --- |
| board_materials | board | sheet | cost_per_sheet | brand, colour, substrate, finish | thickness_mm | thickness_mm, sheet_len_mm, sheet_wid_mm |
| edging_materials | edging | roll | cost_per_unit | brand, colour, edge_type | edge_width_mm | edge_width_mm, edge_thickness_mm, roll_length_m |
| glass_materials | glass | ea | cost_per_unit | colour, glass_type | thickness_mm | thickness_mm, length_mm, width_mm |
| timber_materials | timber | m | cost_per_unit | brand, colour, finish | thickness_mm | thickness_mm, width_mm, length_mm |
| benchtop_materials | benchtop | ea | cost_per_slab | brand, colour, finish | thickness_mm | thickness_mm, slab_len_mm, slab_wid_mm, bench_length_mm, bench_width_mm |
| hardware_materials | hardware | ea | cost_per_unit | brand, hardware_type | - | - |
| appliances | appliance | ea | cost_per_unit | brand, product_type | - | - |
| custom_made | custom | ea | cost | brand, product_type | - | - |
| equipment_hire | hire | ea | rate | - | - | - |

Two column notes that a one-to-one mapping would get wrong:

* The primary key is `material_id` on all eight material tables but **`hire_id`** on
  `equipment_hire`; the mapping carries `pk:` per table so the identity string stays honest.
* `is_active` exists on only five tables (table above), so the E8 rule applies where it exists.
  `brand` exists only on `hardware_materials`; the other tables carry the manufacturer in
  `manufacturer`, `laminate_supplier` or `vendor`.
* `equipment_hire.rate` is a hire rate in `rate_unit` (`DAY`), not a per-unit cost. It is imported
  as the price **only if** a `day` unit is added to `schema/seed.sql`; otherwise the row imports
  with no price and keeps `rate`, `rate_unit` and `total_cost` in `attributes`. This is open
  question 2.

Common field rules (all tables):

| UMR field | Source | Rule |
| --- | --- | --- |
| `name` | `description` | trimmed; required - a row with an empty description fails |
| `product_type` | `product_type`, else the tab-only map, else the category display name | from `mapping/joineryflow.yaml` section 1 |
| `brand_id` | `brand \| manufacturer \| laminate_supplier \| vendor` | first non-null; `party(kind='brand')`; null when all are null. Only `hardware_materials` has a `brand` column, so the others read the table-specific column |
| `default_supplier_id` | `default_supplier \| supplier \| vendors.name via default_supplier_id` | first non-null; `party(kind='supplier')` |
| `default_supplier_ref` | `default_supplier_id` | kept as text for round-tripping |
| `lead_time_days` | `default_lead_time_days`, else `lead_time_days` | integer |
| `unit` | row `um`, else the table default | must exist in `unit`; an unknown code is a row error |
| `unit_cost` | `COALESCE(unit_cost, <own cost column>)` | numeric(14,4); also written as the newest price row |
| `currency`, `cost_basis` | `AUD`, `ex_gst` | constants |
| `product_url` | `product_website` | |
| `synonyms` | `synonyms` | `text[]`, deduplicated case-insensitively, source order kept |
| `description` | `notes` | free text |
| `status` | `archived_at` / `is_active` | see E8 |
| `attributes` | every column not claimed above | keys keep their source names, so write-back stays mechanical |
| `source_system`, `source_id` | constants | E9 |
| `confidence` | `1.00` | manual import is a human action |

Measures are written with `source_system='joineryflow'`, `source_ref='<table>.<column>'`, and
`is_preferred=true`. A derived value (thickness parsed from a name) uses
`source_ref='<table>.description#size'` and adds a warning to the report.

## 8. Sparse data: derived measures and family fallback

Two rules exist because the real rows are sparse, not because the model needs them.

**Derived thickness.** When `thickness_mm` is null and `description` starts with a size token
(`^(\d+(?:\.\d+)?)\s*mm\b`, case-insensitive), the importer writes that number as
`thickness_mm` in `mm` with `source_ref='<table>.description#size'` and a warning. On the current
snapshot this gives ten board rows a thickness (`18, 18, 19, 16, 19, 12, 32, 16, 19, 25`), which is
what makes `measures={"thickness_mm": {"gte": 17.5, "lte": 18.5}}` return rows and gives the family
axis something to vary. It never overrides a real column value.

**Family fallback.** A family key is only used when at least one of its parts is non-null. If every
part is null (the common case here: no colour, no substrate, no finish), the key becomes the
description with the leading size token removed, lower-cased and collapsed (`18mm White MDF` ->
`white mdf`), and the axis stays `thickness_mm`. If that string is empty too, the row gets no family
(`family_id` stays null) and the report counts it as `family_none`. The family key is prefixed with
the table and category (`joineryflow:board:white mdf`) so two tables can never share a family id.

Consequence worth stating: with this snapshot every live row except the hire row becomes its own
**single-member family** (10 board, 4 hardware, 1 benchtop, 1 custom, 1 appliance -> 17 families),
because no two rows share a stripped name. `expand=variants` therefore shows `variant_count: 1`
everywhere. That is honest: the real catalogue has no colour/substrate columns filled in, so it has
no variants of anything yet.

## 9. The importer

New module `ingest/joineryflow.py` plus CLI `scripts/import_joineryflow.py`:

```bash
python scripts/import_joineryflow.py --dsn "$ML_DSN" \
  --snapshot fixtures/joineryflow-hartwood.jsonl --mapping mapping/joineryflow.yaml
python scripts/import_joineryflow.py --dsn "$ML_DSN" --dry-run --json report.json
```

* `--snapshot` defaults to the mapping's `source.snapshot`; `--mapping` defaults to
  `mapping/joineryflow.yaml`; `--dsn` defaults to `$ML_DSN`.
* `--dry-run` builds and reports without writing; `--limit N` imports the first N rows;
  `--include-archived` overrides the header flag (and warns when the file has none).
* `--json` writes the full report; `--quiet` prints one line.
* Exit codes: `0` success, `1` row errors above zero or a database failure, `2` usage.
* One transaction for the whole file, rolled back on failure, per `mapping/joineryflow.yaml`'s
  all-or-nothing batch convention.

Reading and building are pure functions (`read_snapshot`, `build_drafts`) shared with the tests;
only `apply()` touches the database. `build_drafts` returns the existing `Draft` objects so the
write path is unchanged.

`Draft` and `apply()` gain optional fields, all defaulted so the supplier importers are unaffected:
`description`, `synonyms`, `lead_time_days`, `product_type`, `product_url`, `source_system`,
`source_id`, `source_payload`, `status`, `model_no`. `apply()` writes them when present, and keeps
its current behaviour otherwise (including "one preferred measure per key" and the price upsert).

Report shape (`--json`): counts per table, `new`/`updated`, rows with errors and warnings, measures
written (split into column and derived), families created, families with one member, prices written,
and per-table `attributes_empty` so sparse data is visible rather than surprising.

## 10. Verification

Three layers, all runnable without joineryflow:

1. **Unit tests** (`tests/test_etl.py`): `read_snapshot` (header required, unknown `kind` rejected,
   duplicate pk rejected), `build_drafts` on synthetic rows (name from description, derived
   thickness, family fallback, identifier collision handling, status from `archived_at`/`is_active`,
   unknown unit is an error, `include_archived`), and the exporter's row-shaping helpers.
2. **Contract** (`scripts/etl_contract.sql`, a `DO $$` block like `api_contract.sql`): after a real
   import into a fresh database - 18 materials, 0 archived, category spread 10/4/1/1/1/1, ten
   `thickness_mm` measures, `material_search('18mm')` finds both 18 mm boards,
   `measures={"thickness_mm":{"gte":17.5,"lte":18.5}}` returns exactly 2, 17 single-member
   families with every `variant_values` key declared in `material_family.axis_keys`, 14 price rows
   with `source_system='joineryflow'`, `source_payload` present on all 18, and a second import
   changing nothing (`new = 0`).
3. **CI**: the existing `api` job gains a step that applies the schema, runs
   `scripts/import_joineryflow.py` against the committed snapshot, then `scripts/etl_contract.sql`,
   then the pytest suite (which now includes `tests/test_etl.py`).

## 11. Acceptance criteria

1. `python scripts/export_joineryflow.py --workspace hartwood-joinery --out <tmp>` reproduces the
   committed fixture byte for byte (excluding `exported_at`).
2. Importing the fixture into a fresh database yields 18 materials, 0 archived, 6 categories.
3. Ten board rows have `thickness_mm` in `material_measure`, and every derived value carries a
   `source_ref` ending in `#size` and a warning in the report.
4. `material_search` with a measure window returns only matching boards in `mm`, `cm` and `m`.
5. A second import reports `new = 0`, `updated = 18`, and leaves `rev` and `sync_seq` of untouched
   fields unchanged.
6. `--include-archived` on a re-export yields 28 rows and the ten `E2E-*` rows import as `archived`
   (not visible to search).
7. `pytest`, `check_english.py`, `api_contract.sql` and `merge_contract.sql` stay green.

## 12. Risks and non-goals

| Risk | Impact | Mitigation |
| --- | --- | --- |
| joineryflow's columns drift | The exporter breaks on a renamed column | It names the table and column in the error, and the snapshot is committed, so a diff shows exactly what changed |
| Name-derived thickness is wrong for odd names | A filter returns the wrong board | Every derived value is warned and marked `source_ref`; a real `thickness_mm` column always wins |
| Sparse attributes make families single-member | `expand=variants` shows little today | Documented (section 8); the fallback still groups the 18 mm boards with the same decor |
| A snapshot with another workspace is imported | Two catalogues mixed in one database | Header check refuses; UMR v2 has no namespace column, so this is the only guard |
| `source_payload` duplicates the row | Database size | Only the nine catalogue tables, and only rows that changed are rewritten |

Non-goals for this task: writing back to joineryflow (T3), incremental sync (`/v1/changes` already
serves it), images, CV/OCR matching, ETIM/eCl@ss codes, multi-workspace partitioning, and the
supplier price lists (`mappings/laminex.yaml` stays the path for those).

## 13. Delivery

| Step | Work | Done when |
| --- | --- | --- |
| 0 | `scripts/export_joineryflow.py`, `fixtures/joineryflow-hartwood.jsonl` | The fixture is committed, 18 rows + 2 cv lines, and a re-export is identical except `exported_at` |
| 1 | `mapping/joineryflow.yaml` v2 | Every table in section 7 resolves; the mapping parses and `--dry-run` reads it |
| 2 | `ingest/joineryflow.py` (read + build) and the `Draft` field extension | `--dry-run --json` on the fixture reports 18 rows, 0 errors, 10 derived thicknesses, 17 families, 14 prices |
| 3 | `apply()` + CLI | Import into a fresh database is a no-op on the second run (`new = 0`) |
| 4 | `scripts/etl_contract.sql`, `tests/test_etl.py`, CI step | Contract and suite green in the local simulation and in CI |
| 5 | Docs: `docs/etl-joineryflow-progress.md`, README status, `docs/data-model-v2.md` 5/6 | `check_english.py` clean; PR opened |

Effort: about one day, most of it in the mapping and the contract.

## 14. Open questions

Answered by the project owner before this document: snapshot-first (E1), all nine tables with
archived skipped by default (E2), dimensions in `material_measure` (E3), families generated (E4).

Still open, non-blocking:

1. Should the ten archived `E2E-*` rows be in the committed fixture at all? Current plan: not in the
   default file; `--include-archived` regenerates them for the acceptance check in section 11.6.
2. `equipment_hire` has a rate and a rate unit (`DAY`) but no quantity; v1 imports `rate` as the
   price and `rate_unit` as an attribute. Is that the intended commercial reading?
3. `cv_material_mapping` has two rows today. Import them as `cv_code` identifiers now, or leave the
   table for a later task once someone supplies a real CV export?
