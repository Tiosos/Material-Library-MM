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
| 3 | `material_merge()` + `material_duplicate_ignore()` | [x] |
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

## Step 3 - merge and ignore

Shipped in `schema/merge.sql`:

- `material_merge(p_uid_a, p_uid_b, p_keep, p_reason DEFAULT NULL, p_dry_run boolean DEFAULT
  false, p_actor text DEFAULT NULL) RETURNS jsonb` - the ordered algorithm from section 6: lock
  both rows in uid order, validate, build the plan, stop at `p_dry_run`, set `ml.actor` and
  `ml.source`, one UPDATE on the keeper, move the children, supersede the survivor, mark the
  candidate rows, write two merge events, return the report.
- `material_duplicate_ignore(p_uid_a, p_uid_b, p_reason DEFAULT NULL, p_actor text DEFAULT
  NULL) RETURNS jsonb` - records `ignored` with the decision columns, or `conflict` when the pair
  is already merged, or `not_found` when a uid is unknown.

Deviations worth recording:

1. **Business outcomes are data, not exceptions.** `material_merge` raises SQLSTATE 22023 only
   for caller mistakes (a `keep` outside the pair, a reason over 500 characters). "not found",
   "already merged" and "conflict" come back as `{ok, code, ...}` so the HTTP layer in step 4 can
   map them to 404/409 without parsing message text, and so a retry is not an error path.
2. **Idempotency first.** A second call for the same pair returns `code: already_merged` with the
   same `keep`/`merged` and creates no events. The algorithm checks this before it locks deeply,
   so a client can retry safely after a dropped connection.
3. **One UPDATE on the keeper.** The keeper's field changes are assembled into a single
   `UPDATE material SET ...`, so one merge is one `material_revision` row and one event, not
   twenty.
4. **Measure demotion is the interesting rule.** When the survivor carries a preferred row for a
   key the keeper also has, the survivor row is kept as `is_preferred = false` with its original
   `source_system`/`source_ref` instead of being deleted. The keeper's value wins, the merged
   value stays auditable, and the partial unique index still allows exactly one preferred row.
5. **Price collisions follow the index, not the numbers.** A survivor price is dropped only when
   `(supplier_id, valid_from, qty_break, cost_basis)` already exists on the keeper - the columns
   of the unique index. Dropping on `unit_cost` equality would have let a row through that still
   violated the index.
6. **Relations between the pair are deleted, not repointed into a self-loop.** `material_relation`
   has `CHECK (from_uid <> to_uid)`, so the two links between the pair are removed before the
   remaining ones are repointed.

Verification:

| Command | Expected | Actual |
| --- | --- | --- |
| `material_merge(..., p_dry_run => true)` | `code dry_run`, nothing written | `dry_run`, materials 45 -> 45, superseded 1 -> 1 |
| `material_merge('DXPK01','DXPK02','DXPK01', reason, false, 'tester')` | `code merged` | `merged`, `anchors 2`, warnings `commercial_mismatch`, `measure_conflict` |
| keeper after that merge | active, richer record | `active`, name `Fixture Dup Smoked Veneer Panel`, `unit_cost 55.0000`, identifiers `FX-DUP-VEN-22` + `FX-DUP-VEN-22A`, measures `22 mm` preferred + `2.2 cm` not preferred, 2 prices, relation to `BRD018` |
| survivor after that merge | tombstone with no children | `superseded`, `superseded_by DXPK01`, 0 identifiers/measures/prices |
| `material_search` for the survivor | hidden | 0 rows |
| `material_detail(survivor)` | still resolvable | `status superseded` |
| candidate row | `merged` with decision | `merged`, `keep_uid DXPK01`, `decided_by tester`, note kept |
| `material_event` for the pair | 2 merge anchors | 2 rows, `entity material_merge` |
| same call again | `code already_merged` | `already_merged` |
| `material_merge` with a third uid | SQLSTATE 22023 | `invalid_parameter_value` |
| `material_merge` unknown pair | `code not_found` | `not_found` |
| reverse direction `keep=DXPK02` | keeper keeps its own preferred value | keeper `22 mm` preferred, merged `2.2 cm` demoted, prices and identifiers moved |
| `material_duplicate_ignore('DXPK05','DXPK06')` | `ignored` with decision | `ignored`, `decided_by anonymous`, score 1.0000 |
| `material_duplicate_scan()` after that ignore | does not requeue | `pairs 0`, row still `ignored` |
| `material_duplicate_ignore` on a merged pair | `conflict` | `conflict`, blocker `already_merged` |
| ignore with a 501 character reason | SQLSTATE 22023 | `reason must be at most 500 characters` |
| `python .tmp/run_sql.py scripts/merge_contract.sql --db ml_t25` (fixture) | OK (5 assertions) | `OK scripts/merge_contract.sql` |
| same on `ml_t26` (fixture + samples) | OK | `OK scripts/merge_contract.sql` |
| `python -m pytest -q tests -p no:cacheprovider` on `ml_t27` (`both`) | 21 passed | `21 passed, 37 warnings in 2.02s` |
| `python scripts/eval_search.py --dsn .../ml_t28 --dataset fixture` | PASS | PASS, holdout MRR 1.000 / hit@1 1.000 / zero 0.0% / level 100% |
| `python .tmp/run_sql.py scripts/api_contract.sql --db ml_t27` | OK | `OK scripts/api_contract.sql` |
| `python scripts/check_english.py` | OK | `OK: no CJK text found` |

The contract now covers the whole decision path: scan (1-2), preview and queue (3), merge (4) and
ignore (5), including "preview writes nothing" and "a merge is exactly one search result fewer".

## Step 4 - HTTP routes and pytest

Routes added to `api/main.py`, all behind `require_token` and none of them logging a search:

| Route | Behaviour |
| --- | --- |
| `GET /v1/duplicates` | `material_duplicate_queue`, `status` defaults to the queue default (`actionable` = open + ignored), `limit` 1..100, cursor paging, response `{schema_version, status, total, offset, limit, rows, next_cursor}` |
| `GET /v1/duplicates/{uid_a}/{uid_b}` | `material_merge_preview`; 404 `not_found` for an unknown pair, no writes |
| `POST /v1/duplicates/{uid_a}/{uid_b}/merge` | body `{keep, reason?, dry_run?}`; 200 report with `code` `merged`/`dry_run`/`already_merged`, 404 `not_found`, 409 `conflict` (blockers in `detail`), 400 `bad_request` (keeper outside the pair, reason over 500 characters) |
| `POST /v1/duplicates/{uid_a}/{uid_b}/ignore` | body `{reason?}` or no body; 200 `ignored`, 404, 409 when the pair is already merged |

The mapping from a SQL decision to an HTTP answer is one table (`_DUPLICATE_FAILURES`), so a new
business code cannot silently become a 500.

`tests/test_merge.py` (13 tests) is HTTP-level like `tests/test_api.py`. Deviation from section
10.3 of the design, recorded here: the tests cannot share the read-only session connection,
because the app owns its pooled connections and would not see a transaction opened by the test.
They insert their own pair with random Crockford-base32 uids, scan it with
`material_duplicate_scan()` (the routes never scan), and assert against the database rather than
against constants, so the suite is re-runnable on a dirty database. The leftover probe records are
harmless: every assertion compares with the database, and CI recreates it per run.

Verification:

| Command | Expected | Actual |
| --- | --- | --- |
| `GET /v1/duplicates?status=all&limit=1` then follow `next_cursor` | every pair exactly once | 719 pairs walked, no repeat (the assertion collects `(uid_a, uid_b)`, not `uid_a` - one record can pair with several) |
| `GET /v1/duplicates?status=nonsense` | 400 | 400 `bad_request` |
| `GET /v1/duplicates/{a}/{b}` | 200 with plan | 200, `keep`/`suggestion` in the pair, `can_merge true`, `material_event` unchanged |
| `GET /v1/duplicates/mat_nope1/mat_nope2` | 404 | 404 `not_found` |
| `POST .../merge {"dry_run": true}` | 200 `dry_run`, no write | `dry_run`, `superseded` count unchanged |
| `POST .../merge {"keep": ...}` | 200 `merged`, anchors 2 | `merged`, search `total` -1, survivor `superseded`/`superseded_by`, absent from results, still resolvable through `/v1/materials/{uid}` |
| same body again | 200 `already_merged`, no new event | `already_merged`, event count unchanged |
| `keep` outside the pair | 400 | 400 `keep must be one of the two pair members` |
| merge of the archived pair | 409 | 409 `conflict` |
| `POST .../ignore {"reason": ...}` | 200 `ignored`, row updated | `ignored`, `decided_by pytest-merge`, note stored |
| ignore unknown pair | 404 | 404 |
| `GET /v1/duplicates` with `ML_API_TOKEN` set | 401 / 200 with bearer | 401 without header, 200 with |
| `python -m pytest -q tests -p no:cacheprovider` on `ml_t31` (`both`) | 34 passed | `34 passed, 51 warnings in 3.02s` |

A real bug found here: `material_duplicate_queue` is correct, but my first paging assertion used
`uid_a` as the identity. One material can be the `uid_a` of several pairs, so the set looked
smaller than the list; the assertion now compares pairs.

## Step 5 - CLI, CI and docs

- `scripts/merge_materials.py` (`--dsn`, `--keep`, `--merge`, `--reason`, `--actor`, `--dry-run`,
  `--json`): calls `material_merge`, prints a one-screen summary (adopted fields, dropped survivor
  values, children moved, warnings), exits 0 for merged/planned/already-merged, 1 when the
  database refuses, 2 without a DSN. `--dry-run` prints the plan and writes nothing.
- `.github/workflows/ci.yml` job `api`: `schema/merge.sql` is applied to every prepared database,
  a fourth database `ml_merge` gets `eval/fixture.sql`, the merge contract runs against
  `ml_fixture` and `ml_api`, and a "Duplicate CLI (scan, dry run, merge, retry)" step drives both
  CLIs and asserts `scan.pairs == 1`, `report.code == "merged"`, `report.anchors == 2`.
  The contracts run before the HTTP tests on purpose: the merge contract mutates the pair it
  merges and the HTTP tests add their own probe records.
- `README.md` status: T2 checked off, next step now the MCP server. `docs/data-model-v2.md` §6:
  B7 is "Done" with a pointer to `schema/merge.sql` and this design.

Verification:

| Command | Expected | Actual |
| --- | --- | --- |
| `python scripts/duplicate_scan.py --dsn .../ml_t36 --quiet` | exit 0 | exit 0 |
| `... --json .tmp/scan.json` | `pairs 1` | `pairs 1` |
| `python scripts/merge_materials.py ... --dry-run` | plan, no write | `dry_run:` with adopted fields, dropped values, children counts |
| `python scripts/merge_materials.py ... --json .tmp/merge_report.json` | `merged`, anchors 2 | `merged`, `anchors 2`, prices moved 2, measures moved 1 / demoted 1 |
| same command again | exit 0, `already_merged` | `already_merged`, exit 0 |
| merge of the archived pair via CLI | exit 1 | `conflict: survivor_status`, exit 1 |
| `python .tmp/run_sql.py scripts/merge_contract.sql --db ml_t33` (fixture) | OK | `OK` |
| same on `ml_t34` (`both`) | OK | `OK` |
| `python .tmp/run_sql.py scripts/api_contract.sql --db ml_t34` | OK | `OK` |
| `python scripts/eval_search.py --dsn .../ml_t33 --dataset fixture` | PASS | `RESULT PASS` |
| `yaml.safe_load(.github/workflows/ci.yml)` | parses, `api` job has 10 steps | parses, 10 steps |
| `python scripts/check_english.py` | OK | `OK: no CJK text found` |

## Result

T2 is implemented end to end: detection (scan with four exclusion rules), review
(`GET /v1/duplicates`), preview, merge behind an explicit keeper, ignore, and both CLIs. All seven
acceptance criteria of `docs/t2-merge.md` §10.4 hold on fresh databases, and the read API, its
contract and the search evaluation gate are unchanged.
