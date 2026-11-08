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
| 5 | Exporter + committed JSONL snapshot | [ ] | not started (no code until implementation is authorised) |
| 6 | `mapping/joineryflow.yaml` v2 | [ ] | not started |
| 7 | `ingest/joineryflow.py` (read + build) | [ ] | not started |
| 8 | `apply()` + import CLI | [ ] | not started |
| 9 | Contract, unit tests, CI step, progress docs | [ ] | not started |

No implementation code has been written for this task. `git diff dfb7699..origin/master --name-status`
reports exactly one added file: `docs/etl-joineryflow.md`.

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

## Constraints carried into implementation

* The importer reads one committed snapshot and nothing else; no CI step may connect to
  joineryflow.
* Default import into a fresh database: 18 materials, 0 archived, 6 categories, 17 single-member
  families, 10 derived `thickness_mm` measures (each with `source_ref` ending `#size` and a
  warning), 13 price rows, 2 `cv_code` identifiers, `source_payload` on all 18.
* A second import must report `new = 0`.
* Any implementation PR must keep `pytest`, `scripts/check_english.py`, `api_contract.sql` and
  `merge_contract.sql` green.
* No code is written until the project owner authorises implementation.

## Open

Nothing blocking. The delivery steps 0-5 in design section 13 are queued behind authorisation.
