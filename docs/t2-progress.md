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
| 1 | Candidate columns are already in step 0; `material_duplicate_scan()` + `scripts/duplicate_scan.py` | [ ] |
| 2 | `material_duplicate_queue()` + `material_merge_preview()` | [ ] |
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
