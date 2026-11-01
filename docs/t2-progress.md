# T2 - duplicate merge: progress log

Plan: `docs/t2-merge.md`. Rule for this log: **a step is only done when a verification command
printed a result that could have failed.** Each step section records the exact command and what
it printed.

Design changes agreed with the operator after the first draft: `docs/t2-merge.md` section 13
(D9 default queue filter `open` + `ignored`, D10 optional `reason`, D11 report carries the plan
and warnings but not a survivor snapshot).

---

## Status

| Step | Work | State |
| --- | --- | --- |
| 0 | Preflight: `schema/merge.sql`, fixture duplicate pairs, fresh database, this log | [x] |
| 1 | Candidate columns are already in step 0; `material_duplicate_scan()` + `scripts/duplicate_scan.py` | [x] |
| 2 | `material_duplicate_queue()` + `material_merge_preview()` | [x] |
| 3 | `material_merge()` + `material_duplicate_ignore()` | [ ] |
| 4 | HTTP routes in `api/main.py` + `tests/test_merge.py` | [ ] |
| 5 | `scripts/merge_materials.py`, `scripts/merge_contract.sql`, CI job step, docs | [ ] |

### Verification environment

| Piece | Value |
| --- | --- |
| Server | PostgreSQL 16.15 portable build in `.pg/`, data in `.pgdata/`, port 55432, `trust` auth |
| Start command | `.pg/bin/postgres.exe -D .pgdata -p 55432 -c listen_addresses=127.0.0.1 -c logging_collector=off` as a background job (not `pg_ctl`, which fails here) |
| Fresh database | `python .tmp/newdb.py NAME [both\|fixture\|blum]` - never `DROP DATABASE` in this sandbox |
| Local libs | `PYTHONPATH=D:\material library\.pylibs` (vendored wheels; pip cannot write here) |

Environment incident during step 0: the session sandbox denied anonymous pipes
(`popen failure: Permission denied`), and PostgreSQL popen-checks its own binary at startup, so
the server could not start and no verification was possible. Fixed by the operator switching the
session file policy to `danger-full-access`; after that `os.popen("echo hi")` works and the
server starts normally. Nothing in the repository depends on that setting - CI runs the same
commands on a stock `postgres:16` service.

---

## Step 0 - preflight

Shipped:

1. `schema/merge.sql` (new): candidate decision columns `keep_uid`, `decided_by`, `decided_at`,
   `note` (all `ADD COLUMN IF NOT EXISTS`) plus `material_duplicate_status_idx (status, score DESC)`.
   Added to the load order in `.tmp/newdb.py` after `schema/api.sql`.
2. `eval/fixture.sql`: party `Fixture Dup`, three duplicate pairs, all guarded so the existing
   search golden cases keep their exact totals:
   - `mat_FX000000000000000000DXPK01` / `DXPK02` - "Fixture Dup Smoked Veneer Panel" both
     present, `board.veneer`, similarity **0.8649**, 22 mm stored as `22.0 mm` on one side and
     `2.2 cm` on the other; the survivor carries two price rows, one relation and its own
     identifier under `fixture_legacy`.
   - `mat_FX000000000000000000DXPK03` / `DXPK04` - "Fixture Dup Fog Veneer Panel", the second
     one `archived`, so the scan must skip the pair; the live one measures 19 mm so it stays
     outside the 17.5-18.5 golden window.
   - `mat_FX000000000000000000DXPK05` / `DXPK06` - "Fixture Dup Edgeband Tape 22mm" in
     `board.veneer` vs `edging`: the decoy that must never pair.
   The first attempt used `DUPAA1`-style ids and was rejected by `material_material_uid_check`:
   the uid alphabet excludes I, L, O **and U** (Crockford base32), so the ids became `DXPK01`-`DXPK06`.
   Fixture is now 45 stored rows: 42 active, 2 archived, 1 superseded.
3. `eval/golden.yaml` and `docs/api-v1.md` §6.3 updated from "39 rows / 37 active" to
   "45 rows / 42 active" (the search evaluation itself is unchanged).

Verification (all commands run from the repo root, `PYTHONPATH` set):

| Command | Expected | Actual |
| --- | --- | --- |
| `python .tmp/newdb.py ml_t4 fixture` | schema 5 files + fixture OK | all 6 `OK`, DSN printed |
| `similarity('Fixture Dup Smoked Veneer Panel', '... 22mm')` | `>= 0.80` | `0.8649` |
| `information_schema.columns` for `material_duplicate_candidate` | new 4 columns | `uid_a, uid_b, score, status, created_at, keep_uid, decided_by, decided_at, note` |
| fixture status counts | 42 / 2 / 1 / 45 | `(42, 2, 1, 45)` |
| `python scripts/eval_search.py --dataset fixture` on `ml_t4` | PASS | `RESULT PASS`, holdout MRR 1.000 / hit@1 1.000 / level 100% |
| `python -m pytest -q tests -p no:cacheprovider` on `ml_t5` (fixture + samples) | 21 passed | `21 passed` |
| `python .tmp/run_sql.py scripts/api_contract.sql --db ml_t5` | OK | `OK scripts/api_contract.sql` |
| `python scripts/check_english.py` | OK | `OK: no CJK text found` |

Two findings worth remembering:

- Adding rows to the fixture moves `expect_total` cases unless the new rows sit in a category,
  brand and measure range no case touches. The 18 mm B-pair row broke `fx-measure-mm-window`
  (4 instead of 3) until it was changed to 19 mm. Any future fixture row must be checked against
  the golden file first.
- `tests/test_api.py` assumes the `both` dataset (`> 50` rows, brand `Laminex`, a family with
  members). Running pytest against a fixture-only database fails three tests for that reason,
  not because of a regression: use `newdb.py NAME both` for pytest and `NAME fixture` for the
  fixture evaluation, exactly as the CI job does with `ml_api` / `ml_fixture` / `ml_blum`.

---

## Step 1 - scan and CLI

Shipped:

1. `schema/merge.sql`: `material_duplicate_scan(p_threshold numeric DEFAULT 0.80, p_limit integer DEFAULT 500)`
   `RETURNS jsonb`, `LANGUAGE sql` (one statement, so the `candidates` CTE is still visible to the
   final `jsonb_build_object`). Returns `{threshold, pairs, inserted, updated, candidates}`; the
   candidate list is what `--json` writes, so the CLI does not need a second query.
2. `scripts/duplicate_scan.py`: `--dsn` (default `ML_DSN`), `--threshold`, `--limit`, `--json`,
   `--quiet`; prints `scanned N pairs, inserted X, updated Y (threshold T)` plus the first 20 pairs;
   exits 2 without a DSN, 1 on `psycopg.Error`, 0 otherwise.
3. `scripts/merge_contract.sql`: psql-runnable `DO $$` block, assertions 1-2 (designed pair queued
   with score >= 0.80; archived pair and cross-category decoy absent; re-scan updates instead of
   inserting, queue length stable). Steps 2-3 append to the same file.

Deviations from the plan, with reasons:

- The candidate decision columns and the index stayed in `schema/merge.sql` (step 0) instead of
  `schema/material.sql`: the merge feature owns its own DDL, and `material.sql` stays a pure
  description of the base tables. The delivery table in `docs/t2-merge.md` now says so.
- The scan grew two exclusion rules the design did not have, both found by running it:
  a pair that disagrees on any `variant_values` key is a variant (16 mm vs 18 mm boards), and a
  pair whose names match after masking digits but differ in the digits is a variant too
  (`shelf pin 1mm` vs `shelf pin 2mm`). Without the second rule the fixture produced 110 pairs
  instead of 1, 109 of them noise from the 22 filler rows. Both rules are now in section 5.3 of
  the design with the numbers that motivated them.
- psycopg sends a Python `int` as `smallint` and a `float` as `double precision`, so the CLI call
  had to be written `material_duplicate_scan(%s::numeric, %s::integer)`; the bare form failed with
  `UndefinedFunction: function material_duplicate_scan(double precision, smallint) does not exist`.

Verification:

| Command | Expected | Actual |
| --- | --- | --- |
| `python .tmp/newdb.py ml_t10 fixture` then assertion script | scan semantics hold | all assertions passed |
| scan run 1 | `pairs 1, inserted 1, updated 0` | `{'pairs': 1, 'inserted': 1, 'updated': 0, 'threshold': 0.8}` |
| scan run 2 | `pairs 1, inserted 0, updated 1` | `{'pairs': 1, 'inserted': 0, 'updated': 1}` |
| queued rows | exactly the designed pair, `open`, score 0.8649 | `[('mat_FX...DXPK01', 'mat_FX...DXPK02', 0.8649, 'open')]` |
| after `status='merged'` on the pair, scan | not re-reported | `{'pairs': 0, 'inserted': 0, 'updated': 0}` |
| after `status='ignored'` and score set to 0.10, scan | status and score untouched | `('ignored', 0.1000)`, `inserted 0, updated 0` |
| scan at threshold 0 on the fixture | decoys still absent | 12 junk pairs, designed pair present, archived and cross-category pair absent |
| `python .tmp/run_sql.py scripts/merge_contract.sql --db ml_t11` (fixture) | OK | `OK scripts/merge_contract.sql` |
| same on `ml_t12` (`both`, fixture + samples) | OK | `OK scripts/merge_contract.sql` |
| `python scripts/check_english.py` | OK | `OK: no CJK text found` |

Note for step 2: `expect_total` in `eval/golden.yaml` was re-checked after the scan runs because
candidate rows are not materials; `eval_search.py --dataset fixture` on a scanned database still
passes (the queue is metadata, not catalogue).

## Step 2 - keeper, plan, preview and queue

Shipped, all in `schema/merge.sql`:

1. `ml_duplicate_keeper(p_uid_a text, p_uid_b text) RETURNS text` - the suggestion, never the
   decision. Ranks by completeness ratio (`v_material_completeness`), then price count, then
   usage, then oldest row, then uid, and returns one uid.
2. `material_merge_plan(p_keep text, p_merge text) RETURNS jsonb` - the field-by-field plan
   described in section 4 of the design: `{keep, merge, fields, dropped, warnings, children}`,
   with `fields` holding only the keys that actually change, `dropped` recording what the
   survivor's value replaces, and `children` counting what will move or vanish for identifiers,
   measures, prices, relations, classifications, EPD and usage.
3. `material_merge_preview(p_uid_a text, p_uid_b text, p_keep text DEFAULT NULL) RETURNS jsonb` -
   read-only. Defaults `keep` to the suggestion, rejects a third uid with SQLSTATE 22023, and adds
   `{suggestion, pair, blockers, can_merge}` to the plan. Blockers: `keeper_status`,
   `survivor_status`, `category_mismatch`, `already_superseded`.
4. `material_duplicate_queue(p_status text DEFAULT 'actionable', p_limit integer DEFAULT 50, p_offset integer DEFAULT 0)`
   `RETURNS jsonb` - the review list: `actionable` (open + ignored), `open`, `ignored`, `merged`
   or `all`; anything else raises 22023. Rows carry both names, category, units and costs plus
   `keep_suggestion`; the window's `total` is lifted to the payload and stripped from the rows.
5. `scripts/merge_contract.sql` assertions 3 - preview returns a plan for the designed pair,
   refuses the archived pair with a `*_status` blocker, writes no `material_event`, and the queue
   defaults to `actionable` with a `total` equal to the candidate count and a suggestion drawn
   from the pair itself.

Deviations and findings:

- The keeper suggestion prefers the *richer* record, so on the designed pair it returns
  `DXPK02` (two price rows) rather than `DXPK01`. That is the heuristic working, not a bug; the
  contract and tests therefore always pass `keep` explicitly.
- Price collisions in the plan are detected on the unique index key
  `(supplier_id, valid_from, qty_break, cost_basis)`, not on `unit_cost`: two rows could differ in
  price and still collide on the index, which would make a "moved" row a constraint violation.
- Verification on the combined fixture + samples corpus exposed a third scan rule, now in
  section 5.3 of the design: two rows that carry different part numbers of the same kind are
  different parts, even with identical names. The imported Blum file repeats one name
  (`CLIP top BLUMOTION INSERTA hinge NI`) across dozens of hinges that only differ by supplier
  code, so the name-only scan produced 1,011 pairs over 109 rows and filled the 500-row page with
  noise, pushing the designed pair out. With the identifier rule the same corpus yields 3 pairs
  and the designed pair is on page one.

Verification:

| Command | Expected | Actual |
| --- | --- | --- |
| `material_merge_plan('DXPK01','DXPK02')` | adopt/merge/drop per section 4 | fields `{synonyms, attributes}`, dropped `[name, unit_cost]`, warnings `commercial_mismatch`, `measure_conflict`, children `prices moved 2`, `measures moved 1 / demoted 1 / conflicts 1`, `relations from_moved 1`, `identifiers moved 1` |
| `material_merge_plan('DXPK01','mat_nope')` | NULL | NULL |
| `material_merge_preview('DXPK01','DXPK02')` | plan + suggestion + `can_merge true` | keep/suggestion `DXPK02`, `blockers []`, `can_merge true`, `pair.status open` |
| `material_merge_preview` explicit `keep` | keeps the requested side | `keep DXPK02`, `merge DXPK01` |
| `material_merge_preview` on the archived pair | refuses | `can_merge false`, blocker `survivor_status` |
| `material_merge_preview('mat_nope1','mat_nope2')` | NULL | NULL |
| preview twice, compare `material_event` count | unchanged | unchanged |
| `material_duplicate_queue()` | `actionable`, one row | `total 1`, `status actionable`, `keep_suggestion DXPK02`, rows without `total` |
| `material_duplicate_queue('merged')` / `('all')` | 0 / 1 | 0 / 1 |
| `material_duplicate_queue('nonsense')`, bad `keep` | SQLSTATE 22023 | `invalid_parameter_value` both |
| `python .tmp/run_sql.py scripts/merge_contract.sql --db ml_t20` (fixture) | OK (3 assertions) | `OK scripts/merge_contract.sql` |
| same on `ml_t19` (fixture + samples, 4 candidates) | OK | `OK scripts/merge_contract.sql` |
| `python -m pytest -q tests -p no:cacheprovider` on `ml_t21` (`both`) | 21 passed | `21 passed, 37 warnings in 1.93s` |
| `python scripts/eval_search.py --dsn .../ml_t20 --dataset fixture` | PASS | PASS, holdout MRR 1.000 / hit@1 1.000 / zero 0.0% / level 100% |
| `python .tmp/run_sql.py scripts/api_contract.sql --db ml_t19` | OK | `OK scripts/api_contract.sql` |
| `python scripts/check_english.py` | OK | `OK: no CJK text found` |

Cross-database note: the contract and the queue assertions must find the designed pair wherever
it ranks, so the check scans the whole page (`material_duplicate_queue('all', 500)`) instead of
looking at row zero; on the fixture database it happens to be the only row.
