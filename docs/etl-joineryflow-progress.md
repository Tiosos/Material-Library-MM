# Real-data ETL: step log

Design: [etl-joineryflow.md](etl-joineryflow.md). This file records what has been done, what was
verified, and the decisions taken after the design was merged. It is the written log for the ETL
task; the design document itself stays final until implementation reveals a reason to change it.

Rule of the log: a step is "done" when it has a verification line, not when an edit compiles.
Status legend: `[ ]` planned, `[~]` in progress, `[x]` done and verified, `[!]` blocked.

## Status

| # | Step | Status | Verification |
| --- | --- | --- | --- |
| 0 | Preflight: source database, local PostgreSQL, repo state | [x] | joineryflow DB reachable at `127.0.0.1:5432`; workspace `hartwood-joinery` = id 1; local master at `6268ac3` |
| 1 | Measure the real catalogue | [x] | Nine tables queried read-only; 18 live rows, 14 priced, 2 CV rows; see "Source measurement" below |
| 2 | Write and merge the design document | [x] | PR #6 merged 2026-11-06T21:09:57Z as `6268ac3`; CI checks: English-only, schema/demo/import, read API all SUCCESS |
| 3 | Audit the design against schema and live source | [x] | 2026-11-08 read-only audit; schema claims hold, source drift and three internal contradictions found; see "Audit findings" |
| 4 | Resolve the remaining open questions | [x] | Project owner answered all three; see "Decisions" |
| 5 | Exporter + committed JSONL snapshot | [x] | 2026-11-08: `scripts/export_joineryflow.py` + `fixtures/joineryflow-hartwood.jsonl` (21 lines, 15733 bytes) in PR #9; see "Step 0" below |
| 6 | `mapping/joineryflow.yaml` v2 | [x] | PR #10 merged as `1b16c5d`; `scripts/check_joineryflow_mapping.py` verifies nine tables, 18 rows, 10 derived measures, 13 prices, 2 CV codes, 17 families; see "Step 1" |
| 7 | `ingest/joineryflow.py` (read + build) | [x] | PR #11 merged as `a998165`; `python -m ingest.joineryflow --dry-run --json` reports 18 rows, 18 OK, 0 errors, 10 derived measures, 17 families and no contract mismatches; see "Step 2" |
| 8 | `apply()` + import CLI | [x] | PR #12 merged as `191cb42`; fresh PostgreSQL 16 import `new=18`, re-import `new=0 updated=18`, 0 errors; see "Step 3" |
| 9 | Contract, integration tests and CI step | [x] | PR #13 merged as `62af24f`; `scripts/etl_contract.sql` green, `tests/test_etl.py` 6/6 with `ML_ETL_DSN`, dedicated `etl` CI job green with all four checks; see "Step 4" |
| 10 | Docs: this log, README status, `docs/data-model-v2.md` 5/6 | [~] | `check_english.py` clean and the full suite green locally (159 passed, 4 skipped); PR to be opened (design section 13) |

PR #9 was the first implementation PR: exactly two files (the exporter and the snapshot), with no
schema, importer, mapping, CI or README change. PR #8 (this log) was documentation-only. Steps 1-4
followed as PRs #10-#13, all merged into `master` (currently `62af24f`). The design document itself
is unchanged (decision D1); the differences between its prose and the delivered contract are
recorded in "Implementation decisions" below rather than edited into the design.

## Source measurement

Read-only queries against `joineryflow` (workspace 1, `hartwood-joinery`), re-run on 2026-11-08 to
check the numbers in `etl-joineryflow.md` section 1:

| Table | Rows | Live | Priced live | Primary key | Own cost column |
| --- | --- | --- | --- | --- | --- |
| `board_materials` | 10 | 10 | 6 | `material_id` | `cost_per_sheet` |
| `edging_materials` | 0 | 0 | 0 | `material_id` | `cost_per_unit` |
| `glass_materials` | 0 | 0 | 0 | `material_id` | `cost_per_unit` |
| `timber_materials` | 0 | 0 | 0 | `material_id` | `cost_per_unit` |
| `benchtop_materials` | 1 | 1 | 1 | `material_id` | `cost_per_slab` |
| `hardware_materials` | 4 | 4 | 4 | `material_id` | `cost_per_unit` |
| `appliances` | 1 | 1 | 1 | `material_id` | `cost_per_unit` |
| `custom_made` | 1 | 1 | 1 | `material_id` | `cost` |
| `equipment_hire` | 1 | 1 | 1 | `hire_id` | `rate` |
| **Total** | **18** | **18** | **14** | | |

`cv_material_mapping`: 2 rows (`18-PB` -> `board_materials` 1, `700.0KC2.054.00` ->
`hardware_materials` 2). Columns are `cv_code`, `target_material_table`, `target_material_id`.

Board sparseness confirmed on all ten live rows: `thickness_mm`, `colour`, `substrate`, `finish`,
`supplier`, `um` all NULL; `unit_cost` and `cost_per_sheet` NULL on four of ten. Thickness exists
only as the leading token of `description` (`18mm White MDF` etc.), which is the reason for the
derived-measure rule in design section 8.

Live board descriptions, in primary-key order, all match `^(\d+(?:\.\d+)?)\s*mm\b`:
`18mm White MDF`, `18mm Birch Ply`, `19mm Melamine White`, `16mm Melamine Black`,
`19mm Walnut Veneer`, `12mm Birch Plywood`, `32mm MDF`, `16mm Black Melamine`,
`19mm A-Grade Walnut Veneer / BAM X`, `25mm Stainless 304 Sheet`.

## Audit findings

The design document's schema claims were checked against `schema/material.sql` (602 lines) and
`schema/seed.sql`:

* Confirmed: `material.status` CHECK values, `source_payload`/`source_id`, `variant_values`,
  `material_identifier` PK `(kind, value, source_system)` with `legacy_id` and `cv_code` in the
  CHECK, `material_measure` PK `(material_uid, key, source_system, valid_from)` plus the unique
  preferred index, `material_price` unique index `(material_uid, supplier_id, valid_from,
  qty_break, cost_basis)`, and the unit seed list (no `day`).
* Confirmed: `image_blob` is `bigint` on the board table (a pointer, not data), so E10 holds;
  `equipment_hire` uses `hire_id`, so the per-table `pk:` rule holds.

Three discrepancies were found. Per decision D1 below the merged design is **not** rewritten; they
are recorded here as implementation constraints.

1. **Source drift on archived rows.** Design sections 1, 11.6, 12 and 14 assume ten archived
   `E2E-*` automation rows (20 board rows total, 28 with `--include-archived`). The live database
   no longer contains them: all ten board rows are live and no row in any of the nine tables has
   `archived_at` set. The acceptance check in 11.6 therefore cannot be reproduced by re-exporting
   from the live database today; it can only be exercised against a snapshot that still carries
   those rows. The committed fixture, not the live database, remains the CI contract (design
   principle 1).
2. **Price count 13 vs 14.** The design's default reading (section 7, "no `day` unit in
   `schema/seed.sql`" means the hire `rate` stays in `attributes`) yields 13 price rows, while
   sections 10, 11.4 and 13 state 14. Decision D2 resolves this: the correct default is **13**,
   and the contract and step-2 dry-run expectation in the design must be read as 13.
3. **Family key column names.** Section 7 lists `brand` in the family key for
   `board_materials`, `edging_materials`, `glass_materials`, `timber_materials` and
   `benchtop_materials`, but only `hardware_materials` has a `brand` column. Those tables carry
   the manufacturer in `colour`, `edge_type`, `glass_type`, `finish`, `material_type`,
   `laminate_supplier`, `manufacturer` or `vendor` instead. Functionally this is harmless because
   section 8's fallback fires when every key part is null, and all such columns are null on the
   live rows; the mapping must still use real column names when it is written. Section 12's claim
   that the fallback "still groups the 18 mm boards with the same decor" is inaccurate: with this
   snapshot every live row except the hire row is its own single-member family, as section 8
   itself states.

## Decisions

Confirmed by the project owner before the design was merged: E1 snapshot-first, E2 all nine tables
with archived skipped by default, E3 dimensions in `material_measure`, E4 generated per-table
families.

Confirmed on 2026-11-08 in answer to the three remaining open questions:

* **D1 Keep the design final.** `etl-joineryflow.md` stays as merged (PR #6). The discrepancies
  above are recorded in this log rather than edited into the design; CI depends on the committed
  snapshot, not on the live source.
* **D2 `equipment_hire.rate` goes to `attributes` only.** `rate`, `rate_unit` and `total_cost` are
  kept as attributes; the row imports with no price and no `day` unit is added to `seed.sql`.
  Default expected price count is **13**, not 14. This is the commercial reading of E3/"never
  invent commercial data" (principle 5).
* **D3 Import the CV codes now.** The two `cv_material_mapping` rows become
  `identifier(kind='cv_code', value=<cv_code>, source_system='cv')` attached to the material whose
  `source_id` is `<target_material_table>:<target_material_id>`, per E11.

Confirmed on 2026-11-08 in answer to the "next step" questions:

* **D4 Implementation authorised; step 0 first.** The exporter and committed snapshot are written
  as one PR, ahead of the mapping, importer and CI work.
* **D5 Archived-path acceptance uses synthetic rows.** The ten `E2E-*` rows are gone from the
  live database (audit finding 1). The committed fixture keeps the 18 real live rows; the
  `--include-archived` / `status='archived'` path is verified with synthetic rows in unit and
  contract tests, not with fabricated rows in the real fixture.
* **D6 Merge PR #7 before step 9.** PR #7 (the 25HAW import) also edits `.github/workflows/ci.yml`
  and `README.md`. It should be merged before the ETL CI step is added, to avoid a conflict. Step 0
  does not touch either file, so it does not depend on that merge.

## Implementation decisions (after the design was frozen)

Recorded here under D1; `etl-joineryflow.md` is not edited.

* **D7 JSONL is a byte-level contract on Windows.** The global `core.autocrlf=true` checkout
  rewrote `fixtures/joineryflow-hartwood.jsonl` to CRLF, changing it from 15733 to 15754 bytes and
  breaking acceptance criterion 1. `.gitattributes` now marks `*.jsonl` as `-text` (no EOL
  conversion at all); `text eol=lf` alone was not sufficient. Both tracked JSONL files were
  restored to LF. PR #10.
* **D8 Mapping rules follow the real column names.** Per audit finding 3, the family-key and
  supplier rules use the columns the snapshot actually carries; `custom_made.vendor` was added to
  the supplier fallback chain (the only populated supplier text on that row), and the
  `vendors.name via default_supplier_id` branch was dropped because an importer reading only the
  snapshot cannot resolve a table that is not exported. PR #10.
* **D9 The CI ETL check is a separate job, not a step in `api`.** Design section 10.2 asks for a
  step in the existing `api` job, but its verification demands a fresh database while every
  `api`-job database also carries the evaluation corpus and the supplier demo. The new `etl` job
  uses its own `postgres:16` service, `POSTGRES_DB=ml_etl` and `ML_ETL_DSN` (deliberately not
  `ML_DSN`), so no shared pytest run can assert ETL counts against a populated catalogue. PR #13.
* **D10 Three prose numbers are read as the contract states them.** 13 prices, not 14 (D2);
  11 `thickness_mm` measures in total, of which 10 are derived (`source_ref` ends in `#size`) and
  one comes from a populated column; and `rev` stays 1 after a re-import although `sync_seq` may
  advance on the 11 rows carrying a measure (`ml_child_touch` bumps the parent cursor, but
  `ml_material_before` sees no meaningful parent change). `scripts/etl_contract.sql` asserts all
  three explicitly. PR #13.

## Step 0: exporter and committed snapshot (PR #9)

Files: `scripts/export_joineryflow.py` (303 lines) and
`fixtures/joineryflow-hartwood.jsonl` (21 lines, 15733 bytes).

Verification run on 2026-11-08 against the live source, all passing:

* Determinism (acceptance criterion 1): a second export with `--quiet --force` is byte-identical
  to the committed fixture on every line; the header differs only in `exported_at`.
* Format: 21 LF lines, sorted keys, valid header (schema version 1, `joineryflow`,
  `hartwood-joinery`, `include_archived: false`, nine tables in contract order), 18 `row` lines +
  2 `cv_mapping` lines, no `image_blob`, table order and primary-key order correct, per-table
  counts 10/0/0/0/1/4/1/1/1, all ten board descriptions carry a leading mm token
  (`18, 18, 19, 16, 19, 12, 32, 16, 19, 25`).
* CLI guards: `--out` outside `fixtures/` refused without `--force` (exit 2), unknown workspace
  exit 2, `--include-archived` flips the header flag, `--json` report matches the file bytes.
* Pure functions: `shape_value` (Decimal to string, date to ISO, timestamptz to UTC `Z`, list,
  unsupported type raises `ShapeError`), `build_header`, `encode_line`.
* Read-only: `BEGIN READ ONLY` verified to reject a `CREATE TABLE` with
  `ReadOnlySqlTransaction`; no probe table remains; source row counts unchanged.
* `python scripts/check_english.py` clean; `python -m compileall -q scripts/export_joineryflow.py`
  clean; the existing pytest suite is untouched.

Additional preflight measurements recorded for steps 1-2:

* `fixtures/` did not exist before step 0 (created by the exporter).
* `product_type` is NULL on all 18 live rows, so section 7's "tab-only map / category display
  name" fallback is the only effective path.
* The supplier fallback chain in section 7 omits `custom_made.vendor`: that row has
  `default_supplier`, `supplier` and `default_supplier_id` all NULL and only `vendor='Metalform'`.
  **Step 1 must add `vendor` to the chain** or that supplier is lost. The other 17 rows have a
  usable text column.
* `vendors.name via default_supplier_id` is never needed on the live rows and is not implementable
  by an importer that reads only the snapshot (no `vendors` section is exported). Despite the
  real FK values being correct where present, step 1 should drop that branch from the rule.
* 25 non-null `sku` / `code` / `internal_ref` values with **no cross-table duplicates**, so E5's
  uniqueness check will pass cleanly.
* `synonyms` is a populated `text[]` on board and hardware rows, so the `Draft.synonyms`
  extension is exercised by real data.

## Step 1: executable mapping v2 (PR #10)

Files: `.gitattributes`, `mapping/joineryflow.yaml` (rewritten as `version: 2`),
`scripts/check_joineryflow_mapping.py`.

Verification:

* `python scripts/check_joineryflow_mapping.py`: all six non-empty source tables verified against
  the snapshot columns (10/1/4/1/1/1 rows, 34/39/24/23/23/26 known columns); the three empty
  tables are checked as `known_columns`. Measured: 18 rows, 0 archived, 6 categories, 10 derived
  measures, 13 prices, 2 CV codes, 17 families, 30 identifier values, 0 collisions.
* The mapping resolves every table in design section 7, uses real column names (D8), keeps
  `equipment_hire.rate` in `attributes` (D2), imports both `cv_material_mapping` rows as
  `cv_code` identifiers (D3), and declares `import_contract.expected` so the harness and the
  static validator assert the same numbers.
* JSONL byte contract: `git check-attr text -- fixtures/joineryflow-hartwood.jsonl` reports
  `unset`, the file is LF, 15733 bytes, 0 CRLF (D7). `.venv/` was prepared with
  `api/requirements.txt` and `ingest/requirements.txt`.
* `check_english.py`, `compileall`, the schema JSON, `check_mappings.py` and the 40 pre-existing
  unit tests all green at merge; CI `static` job green.

## Step 2: pure reader and draft builder (PR #11)

Files: `ingest/joineryflow.py`, `tests/test_unit_joineryflow.py`, and `ingest/importer.py`
(`Draft` gains optional UMR columns; `apply()` writes them through a whitelist).

Verification:

* `python -m ingest.joineryflow --dry-run --json`: 18 rows, 18 OK, 0 errors, 10 derived thickness
  measures, 17 single-member families, 13 prices, 2 CV identifiers, `source_payload` on all 18.
  `check_contract()` finds no mismatch with `import_contract.expected` (exit 0). `--json` without
  a path prints to stdout; with a path it writes a file; a contract mismatch exits non-zero.
  `--dry-run` is an explicit no-op flag.
* `read_snapshot` rejects a missing or invalid header, unknown `kind`, duplicate primary keys,
  `image_blob`, blank lines and invalid JSON; `build_drafts` is a pure function (no database, no
  file writes); `--include-archived` is exercised with synthetic rows (D5).
* `apply()` optional columns are whitelisted and cast as `%s::jsonb` / `%s::text[]`; supplier
  importers are unaffected because every new `Draft` field is defaulted.
* 80 pure-Python unit tests green at merge; both mapping validators, `check_english.py`,
  `compileall` and the JSONL byte contract green.

## Step 3: import CLI (PR #12)

Files: `scripts/import_joineryflow.py`, `tests/test_unit_import_joineryflow.py`.

Verification:

* Against a fresh PostgreSQL 16 database loaded with `material.sql`, `search.sql` and `seed.sql`:
  first import `new=18 updated=0 skipped=0`, 0 errors, `contract.ok = true`; second import
  `new=0 updated=18 skipped=0`, 0 errors. The database holds 18 materials, 13 prices, 11
  measures, 17 families and 48 identifiers.
* `material_search('18mm')` returns 2; category spread `board:10 hardware:4 benchtop:1
  appliance:1 custom:1 hire:1`; all 13 prices carry `source_system='joineryflow'`; all 18 rows
  have a non-empty `source_payload`; 2 `cv_code` identifiers; 0 archived.
* The CLI is one transaction for the whole file, aborts before connecting on row errors, rolls
  back on a database error, and supports `--dry-run`, `--limit`, `--include-archived`,
  `--json [FILE]`, `--quiet` and an injectable `connect` for tests. 13 new unit tests (93 total).
* The branch was rebased with `git rebase --onto master feat/etl-step2-ingest feat/etl-step3-cli`
  after PR #11 merged, so PR #12 carried exactly one commit.

## Step 4: contract SQL, integration tests and CI (PR #13)

Files: `scripts/etl_contract.sql`, `tests/test_etl.py`, `.github/workflows/ci.yml` (new `etl`
job).

Verification:

* `psql -v ON_ERROR_STOP=1 -f scripts/etl_contract.sql` on a fresh imported database prints
  `etl contract OK: 18 materials, 6 categories, 10 derived measures, 13 prices, 17 families`.
  The DO block asserts active/archived counts, the category spread, the browse total, the
  10-derived + 1-column measure split and the derived values, mm/cm/m measure windows, family
  axis keys, 13 hire-free prices, the hire `rate` living in `attributes`, `source_payload`, CV
  codes, `rev = 1`, 18 revisions and no parent UPDATE events.
* `python -m pytest -q tests/test_etl.py --noconftest`: 2 passed, 4 skipped without
  `ML_ETL_DSN`; 6 passed with it. The full suite on the CI `api` recipe is 159 passed, 4 skipped.
* The CI `etl` job builds `ml_etl` from scratch, asserts `new=18` then `new=0 updated=18`, runs
  the SQL contract and the ETL pytest file. PR #13 had all four checks green
  (`English-only and syntax`, `Schema, demo and import pipeline`, `Read API, contract and search
  evaluation`, `Joineryflow ETL contract`), mergeable state `clean`.
* The local PostgreSQL 16 environment is a Docker container `ml-postgres16` (`postgres:16`,
  volume `ml-pg16-data`, `127.0.0.1:5432`) with
  `ML_DSN=postgresql://postgres:postgres@127.0.0.1:5432/ml`.

## Constraints carried into implementation

These were the rules agreed before step 1. All five are now satisfied and verified by the steps
above.

* The importer reads one committed snapshot and nothing else; no CI step may connect to
  joineryflow. Confirmed: the `etl` job installs only `pyyaml`, `psycopg[binary]` and `pytest`,
  and runs `scripts/import_joineryflow.py` against `fixtures/joineryflow-hartwood.jsonl`.
* Default import into a fresh database: 18 materials, 0 archived, 6 categories, 17 single-member
  families, 10 derived `thickness_mm` measures (each with `source_ref` ending `#size` and a
  warning), 13 price rows, 2 `cv_code` identifiers, `source_payload` on all 18. Confirmed by
  `scripts/etl_contract.sql`; the database also has one extra `thickness_mm` measure from a real
  column (11 total, see D10).
* A second import must report `new = 0`. Confirmed: `new=0 updated=18 skipped=0` in CI and locally.
* Any implementation PR must keep `pytest`, `scripts/check_english.py`, `api_contract.sql` and
  `merge_contract.sql` green. Confirmed on PRs #10-#13: every CI job green on every PR (three
  jobs before the `etl` job existed, four afterwards).
* PR #7 (25HAW import) should be merged before the ETL CI step (D6). Satisfied: PR #7 merged as
  `e974c07` before PR #13 touched `.github/workflows/ci.yml`; no conflict occurred.

## Open

Nothing blocking. Steps 0-4 of design section 13 are complete and merged (PRs #9-#13); `master`
is at `62af24f`. Step 5 (this log, README status and `docs/data-model-v2.md` 5/6) is the
remaining delivery item and is done when `check_english.py` is clean and its PR is opened.

Out of scope for this task, and still open for the project as a whole: the write-back executor
(T3, the approved `material_change_request` has no PATCH executor yet), live incremental sync
from joineryflow, ETIM/eCl@ss codes and EPD collection. See design section 12 for the non-goals
list.
