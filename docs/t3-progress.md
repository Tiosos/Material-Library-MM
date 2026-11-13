# T3 write-back executor: step log

Design: [t3-writeback.md](t3-writeback.md). This file records what has been done, what was
verified, and the decisions taken after the design was written. It is the written log for the
write-back task; the design document stays final except for the implementation notes that record
facts the code revealed.

Rule of the log: a step is "done" when it has a verification line, not when an edit compiles.
Status legend: `[ ]` planned, `[~]` in progress, `[x]` done and verified, `[!]` blocked.

## Status

| # | Step | Status | Verification |
| --- | --- | --- | --- |
| 0 | Design record `docs/t3-writeback.md` and mapping-validator extension | [x] | PR #15 merged as `cbf1b5f`; 482-line design (33358 bytes), `scripts/check_joineryflow_mapping.py` asserts the `write_back` allowlist, nine endpoints and per-table attributes; see "Step 0" below |
| 1 | `schema/writeback.sql`: additive columns, projection tables, `submit` / `decide` / `plan` / `queue` | [x] | PR #15 merged as `cbf1b5f`; 45 behavioural assertions on a fresh PostgreSQL 16 database, four CI jobs green; see "Step 1" |
| 2 | `claim` / `settle` / `retry`, five-minute lease, `FOR UPDATE SKIP LOCKED` | [x] | 60/60 database assertions, including two sessions claiming disjoint batches; full suite `159 passed, 4 skipped`; see "Step 2" |
| 3 | `scripts/apply_change_requests.py`, fake upstream and `tests/test_writeback.py` | [x] | `18 passed` with `ML_WRITEBACK_DSN`, `8 passed / 10 skipped` without; full suite `167 passed, 14 skipped`; see "Step 3" |
| 4 | `scripts/writeback_contract.sql`, five HTTP review routes and the `writeback` CI job | [x] | SQL contract green with zero residual rows; 11 HTTP route tests; the three existing api SQL contracts still green; full suite `178 passed, 14 skipped` across five CI jobs; see "Step 4" |
| 5 | Docs: this log, README status and `docs/data-model-v2.md` 5/6 | [x] | PR #16; `check_english.py` clean, full suite green locally (`178 passed, 14 skipped`), 64 relative doc links intact, JSONL byte contract intact; design section 14 |
| 6 | T3 x T2 consistency: merge blocker plus the decide / plan / settle / retry status guards, contract assertions and integration cases (design 6.1) | [x] | Contract green on a fresh PostgreSQL 16 database (both load orders), `tests/test_writeback.py` 21/21 with `ML_WRITEBACK_DSN`, `tests/test_writeback_api.py` 12/12, full suite `179 passed, 17 skipped`, api and merge contracts still green; see "Follow-up 1" |
| 7 | T3 x T3 loop: `settle()` writes the diff into `material`, submit validates storable values, JSON null clears a field, the importer refuses a stale snapshot (design 6.2, D18/D19) | [x] | Contract green with the new assertions; `tests/test_writeback.py` 22/22; full suite `179 passed, 18 skipped`; api and merge contracts green on a rebuilt database; see "Follow-up 2" |
| 8 | Retention and the watermark (design 6.3, D20): `material_writeback_state`, a watermark-driven import guard, `scripts/prune_change_requests.py` | [x] | Contract green with section 14; `tests/test_writeback.py` 25/25; full suite `179 passed, 21 skipped`; api / merge / etl contracts green; see "Follow-up 3" |
| 9 | `--verify` (design 6.4, D21): `verify_plan()` / `verify()`, shared local-write helpers, `JoineryflowClient.get()`, `--verify` / `--resolve`, contract section 15 | [x] | Contract green with section 15, re-runnable with zero residual rows; `tests/test_writeback.py` 33/33; full suite `180 passed, 28 skipped`; api / merge / etl contracts and static checks green; see "Follow-up 4" |
| 10 | Field coverage (design 7, D22): contract sections 16 and 17, the `plan` / `verify_plan` key agreement, the attributes namespace and the tombstone decision | [x] | Contract green with sections 16-17, re-runnable with zero residual rows and no tombstone left behind; `tests/test_writeback.py` 37/37 twice in a row; full suite, api / merge / etl contracts and static checks green; see "Follow-up 5" |

Steps 0-1 were merged in **PR #15** (merge commit `cbf1b5f`, 2026-11-11T22:01:22Z). Steps 2-5
were implemented and verified on top of that commit and are delivered together in **PR #16**.
The design document itself was not rewritten; the differences between its prose and the delivered
contract are recorded in "Implementation decisions" below.

## Source measurement

Preflight facts established before any code was written, by reading the shipped repository rather
than the design prose:

| Question | Measured answer |
| --- | --- |
| Does `material_change_request` have any reader or writer? | No. Only `schema/material.sql` (DDL with a five-state CHECK) and one pending demo row in `schema/sample_demo.sql` reference it. |
| What identifies the upstream row to PATCH? | All 18 imported joineryflow rows have `source_id` of the form `<table>:<int>`; each of the nine tables declares its own `endpoint` in `mapping/joineryflow.yaml`. |
| Is the workspace persisted in the UMR? | No. The snapshot header carries `workspace_slug = hartwood-joinery`, but zero of the 18 `source_payload` objects contain that key. |
| Does `material` expose the seven writable fields? | Yes: `name`, `product_type`, `unit`, `unit_cost` (numeric), `lead_time_days` (int), `product_url`, `synonyms` (text[]) and `attributes` (jsonb). |
| Does the ETL's no-op re-import move `rev`? | No. `sync_seq` moves on 11 rows through `ml_child_touch()`, `rev` stays 1 on all 18. |

The last row is why the approval fingerprint is field-level rather than a whole-row or
`sync_seq` comparison.

## Implementation decisions (after the design was written)

* **D1 - `{slug}` is the per-table endpoint, not the workspace.** The nine `endpoint` values in
  the mapping already supply it; `{mid}` is the numeric part of `source_id`. No new locator column
  and no ETL change. The workspace is configuration, checked at executor startup against both
  `source.workspace` in the mapping and `workspace_slug` in the snapshot header.
* **D2 - `schema/writeback.sql` loads after `schema/seed.sql`.** Design section 5 originally said
  "before seed.sql". `material_writeback_catalog.category_code` references `material_category`,
  whose rows `seed.sql` inserts, so that order fails on the foreign key. Both the `api` job and
  the `writeback` job now use `seed.sql` before `writeback.sql`; the design has an implementation
  note recording the correction.
* **D3 - A claim writes no `material_event`.** Acceptance criterion 2 counts exactly three events
  on a happy path (insert, approve, apply). A claim changes no status, only bookkeeping columns,
  so it is not a transition. `settle()` writes the third event.
* **D4 - `retry()` is a SQL function, not CLI logic.** Design section 6 lists `failed -> approved`
  but section 5's function table did not name it. SQL owns every transition (D4 of the design),
  so step 2 added `material_change_request_retry()` and `--retry N` simply calls it.
* **D5 - `claim()` gained an optional `p_only_id`.** The CLI's `--request-id N` must claim exactly
  one request; filtering a batch in Python would either release a lease or write state outside
  SQL. The fourth parameter defaults to NULL, so step 2's behaviour is unchanged.
* **D6 - The SQL contract is transactional.** `scripts/writeback_contract.sql` wraps its `DO`
  block in `BEGIN; ... ROLLBACK;`. It exercises real transitions yet leaves zero request and
  event rows behind, so the integration tests that follow start clean and the file is re-runnable.
* **D7 - Design section 14 expected contract assertions 1-3 in step 1.** They were delivered in
  step 4 together with the HTTP routes and CI job, as one psql-runnable contract covering all
  transitions. Step 1's verification used a temporary assertion script instead; those checks are
  now permanent in the SQL contract.
* **D8 - HTTP route tests build their own material row.** The `api` job's databases carry the
  evaluation corpus and the supplier demo but not the joineryflow snapshot, so
  `tests/test_writeback_api.py` inserts one disposable row with a valid `source_id` and deletes
  it afterwards. Deletion needs `set_config('ml.allow_delete', 'on', false)` because `material`
  has a `BEFORE DELETE` trigger; the fixture also self-heals rows left by an interrupted run so
  the duplicate scan cannot see them.
* **D9 - `jsonb_set` and SQL NULL.** `jsonb_set` returns SQL NULL (not JSON null) when its
  `new_value` argument is SQL NULL, which corrupted `base.values` for a missing attribute.
  `submit()` and `ml_writeback_current_values` now normalise missing values to JSON null so the
  stale check compares byte-equal on both sides.
* **D10 - The demo `unit_cost` diff is legal.** Design section 4.3: updating the catalogue's own
  price is allowed; only a price already copied onto a quote is forbidden. The UMR has no quote
  rows and the allowlist cannot express a path to one, so no special price check was added.

## Step 0: design and mapping validator (PR #15)

`docs/t3-writeback.md` (482 lines / 33358 bytes at merge time) records the state machine, diff allowlist,
target resolution, executor, HTTP edge, test plan, acceptance criteria, decisions D1-D16 and open
questions. `scripts/check_joineryflow_mapping.py` was extended to parse the SQL projection tables
and assert they agree with the mapping: nine endpoints, seven inverse field rows and the
per-table attribute lists. A five-case mutation test (endpoint, pk, attribute, field value,
missing row) confirmed the validator catches drift rather than merely parsing.

Verification: `check_english.py` clean; every relative link reachable; all 13 cited line numbers in
`material.sql`, `merge.sql`, `main.py`, the mapping, `sample_demo.sql` and `ci.yml` checked;
18/18 `source_id` values and the JSONL byte contract (15733 bytes, 0 CRLF) re-measured.

## Step 1: SQL state machine (PR #15)

`schema/writeback.sql` added six columns (`base`, `attempts`, `claimed_at`, `claimed_by`,
`idempotency_key`, `last_status`), the `material_writeback_catalog` and
`material_writeback_field` projections, and `submit` / `decide` / `plan` / `queue`. Caller
mistakes raise SQLSTATE 22023; business outcomes return `{ok, code}`.

Verification: two consecutive loads of the file succeed; 45 behavioural assertions on a fresh
PostgreSQL 16 database with the committed snapshot imported cover server-computed `before`, the
`base` fingerprint, forbidden paths (including the offending path in the response), the
per-table `attributes.<col>` allowlist, `no_change`, `open_request`, archived material ->
`conflict`, self-approval, `already_decided`, `stale_base` with a differing current/base report,
the reject audit payload, plan URL / headers / inverse-translated body, and queue filters and
paging. The full local suite stayed green (`check_english.py`, `compileall`, `check_mappings.py`,
`check_joineryflow_mapping.py`, 93 unit tests, JSONL byte contract). All four CI jobs were green
on PR #15.

## Step 2: claim, settle, lease and concurrency

`claim()` uses a materialised CTE with `FOR UPDATE SKIP LOCKED`, takes only `approved` rows whose
lease is free or expired, increments `attempts` and fixes `idempotency_key = mlcr-{id}`.
`settle()` re-checks the field-level base immediately before recording `applied`; a 2xx on stale
data becomes `failed / applied_unverified`, a 5xx or timeout becomes
`failed / uncertain_outcome`, and a caller that lost its lease cannot settle. `retry()` closes the
last transition in design section 6.

Verification: 60/60 database assertions on a fresh database. Two independent connections each
claimed two rows with no overlap and no blocking (`statement_timeout = 8s` never fired), proving
`SKIP LOCKED`; claim produced zero events and the happy path exactly three; a live lease blocked
re-claiming, an expired one was reclaimed with `attempts = 2`; four settle outcomes, five
parameter-validation errors, four retry branches, plan/queue regression and idempotency-key
uniqueness all passed. Full regression: `check_english.py`, `compileall`, `check_mappings.py`,
`check_joineryflow_mapping.py`, 93 unit tests, complete pytest `159 passed, 4 skipped, 200
subtests`, JSONL `15733 / 78936` bytes with 0 CRLF.

## Step 3: worker CLI and fake upstream

`scripts/apply_change_requests.py` defaults to a true no-op dry run; `--apply` requires
`ML_JF_BASE_URL` and `ML_JF_TOKEN` and exits 2 before opening the database when either is
missing. It claims a batch, calls `plan()` for each row, sends one stdlib `urllib` PATCH outside
any transaction, and settles 2xx / 4xx / 5xx / timeout through SQL. The token is never printed,
never written to a report and never hard-coded; no real upstream URL appears in the source.

`tests/test_writeback.py` starts a stdlib `ThreadingHTTPServer` on 127.0.0.1 with an ephemeral
port, records method / path / headers / body, and can inject status codes and delays. Eight pure
unit tests run without a database; ten integration tests skip unless `ML_WRITEBACK_DSN` is set.

Verification: `18 passed` on a fresh `ml_writeback_ci` database (eight unit + ten integration),
`8 passed / 10 skipped` without the DSN. Cases: dry-run zero writes / zero events / zero HTTP,
happy path (3 events, 1 PATCH, `last_status = 200`), second apply with zero requests, 400 / 409,
503, timeout, stale 2xx -> `applied_unverified`, forbidden paths, retry then apply, and two
concurrent workers producing exactly one PATCH per request. Full suite `167 passed, 14 skipped,
200 subtests`.

## Step 4: SQL contract, review routes and CI

`scripts/writeback_contract.sql` is psql-runnable and rolls back its own work. It asserts the six
additive columns, the 9/7 projection tables, the untouched five-state CHECK, `SKIP LOCKED` in
the function body, the allowlist, every legal transition, the illegality of `pending -> applied`,
staleness at approve and at apply, lease reclaim, one event per transition, retry refusing an
uncertain outcome, and a token-free plan.

`api/main.py` gained five routes: `POST /v1/change-requests`, `GET /v1/change-requests`,
`GET /v1/change-requests/{id}`, `POST .../approve` and `POST .../reject`. There is deliberately
no apply route. `tests/test_writeback_api.py` covers 11 cases including error mapping, cursor
paging, self-approval, stale base, terminal rejection and the absence of an apply route.

CI now has five jobs: `static`, `database`, `api`, `etl` and `writeback`. The `api` job loads
`writeback.sql` after `seed.sql` in all four of its databases; the `writeback` job uses a fresh
`ml_writeback` database, sets only `ML_WRITEBACK_DSN` (no `ML_JF_*` credential), loads the
schema, imports the snapshot, runs the SQL contract and runs
`pytest -q tests/test_writeback.py --noconftest`.

Verification: SQL contract printed `writeback contract OK` with zero residual rows; writeback job
`18 passed`; api job after a fresh rebuild: `api_contract.sql` and `merge_contract.sql` on both
`ml_fixture` and `ml_api` green, full suite `178 passed, 14 skipped, 200 subtests`; static job
`check_english.py` (with `git add -N` for new files), `compileall`, schema JSON,
`check_mappings.py`, `check_joineryflow_mapping.py` and 93 unit tests green; JSONL byte contract
unchanged.

Two local-only pitfalls, neither present in CI: running `merge_contract.sql` after pytest (the
merge tests mutate `ml_api` duplicate pairs) produced a false failure, and an interrupted HTTP
test left nine disposable material rows behind until the fixture was made self-healing. Running
the steps in CI order and cleaning the rows made both disappear.

## Follow-up 1: T3 x T2 consistency

The first gap found after T3 closed: T2 and T3 meet at `material_uid` and nothing guarded the
meeting. `material_merge()` supersedes the loser (T2 D4) and the tombstone keeps its field values,
so T3's field-level `base` fingerprint still matched. A merge that landed after `submit()` let
`decide()` approve and `plan()` build a PATCH against an upstream row another human had just
declared a duplicate. `submit()` refuses a tombstone, but only at submit time.

Neither shortcut was taken. Re-pointing the request at the keeper would change `{mid}`, hence the
upstream record the approver saw; auto-failing requests inside `material_merge()` would add a
`pending -> failed` edge that D13 keeps out of the five-state CHECK. The rule is therefore split
(design 6.1, decision D17):

| # | Function | Guard |
| --- | --- | --- |
| 1 | `material_merge_preview()` (T2) | a `pending`/`approved` request on either side adds the blocker `open_change_request` and lists the ids in `blocking_requests`; `material_merge()` returns 409 `conflict` |
| 2 | `material_change_request_decide()` | `approve` on an `archived`/`superseded` material returns `material_inactive` (409 at the edge); `reject` still works |
| 3 | `material_change_request_plan()` | same check before target resolution; `--apply` already turns a non-ok plan into `settle('failed', error=code)` with no HTTP call |
| 4 | `material_change_request_settle()` / `retry()` | a 2xx on a row merged away mid-flight becomes `failed / material_inactive` and is reported as `applied_unverified`; `retry()` refuses the tombstone |

`claim()` is deliberately not a guard: skipping a tombstone there would leave an `approved` row
nobody can see. Claiming it and letting `plan()` refuse ends it as `failed / material_inactive`,
which shows up in the review queue.

Files: `schema/writeback.sql` (four checks), `schema/merge.sql` (blocker and `blocking_requests`),
`api/main.py` (`material_inactive` -> 409), `scripts/writeback_contract.sql` (section 11),
`tests/test_writeback.py` (three cases), `tests/test_writeback_api.py` (one case), design 6.1/9/12/15.

Verification on a fresh PostgreSQL 16 database (postgres:16 in Docker, 127.0.0.1:5432):

* `scripts/writeback_contract.sql` printed `writeback contract OK` on both load orders - the api
  job's (`material, search, api, merge, seed, writeback` plus `eval/fixture.sql`) and the
  writeback job's. The contract rolls back, so it leaves zero request and event rows.
* `tests/test_writeback.py` 21/21 with `ML_WRITEBACK_DSN` (eight unit + thirteen integration), on
  a database shaped exactly like the CI `writeback` job.
* `tests/test_writeback_api.py` 12/12 and the full suite `179 passed, 17 skipped, 200 subtests` on
  a database shaped like the CI `api` job (`api_contract.sql` and `merge_contract.sql` green
  before it).
* `check_english.py`, `compileall`, `check_mappings.py`, `check_joineryflow_mapping.py` and the 93
  static unit tests are clean; the JSONL byte contract is untouched.

One finding the run produced, which static checks could not: the contract now calls
`material_merge_preview()`, and the `writeback` CI job did not load `schema/merge.sql`. The
minimal-order database failed with `function material_merge_preview(text, text) does not exist`,
so the job now loads `material, search, merge, seed, writeback` and design section 11 records why.
The `api` job already loaded `merge.sql`, which is why only the `writeback` job needed the change.

## Follow-up 2: T3 x T3, the write never reached the master record

Found while closing Follow-up 1, by asking what `applied` actually means. Measured on the fresh
database, in a transaction that was rolled back:

```
status=applied  before=3450.0000  requested_after=3460.0000  live_after_settle=3450.0000
re-submit after applied -> code=submitted  id=50
```

`settle()` recorded the outcome and wrote an event, but nothing ever wrote the approved values into
`material`. The consequences were wider than one stale field: detail, export and search kept
returning the old value until the next snapshot; `base` compared the local row with itself, so the
staleness guard could not see upstream drift; and the same diff could be filed and PATCHed again,
because `submit()` reads `before` from that stale value and `open_request` only covers `pending`
and `approved`.

[design-v1](design-v1.md) line 311 left master-data ownership open; section 0 of the T3 design had
already asserted "Material Library stays the single source of truth". D18 makes the code agree:
`settle('applied')` applies the diffed paths to `material` in the same transaction, after the base
and status checks, and the existing triggers produce one revision and one event. A local write that
fails degrades to `failed / local_write_failed` reported as `applied_unverified` - a 2xx is never
lost because the local row could not take the value.

Two supporting changes were needed:

* **`submit()` now validates what the master row can store** - `unit` must exist in `unit`
  (a foreign key), `unit_cost` must fit `numeric(14,4)`, `lead_time_days` must fit `integer` - so
  the local write cannot fail after the PATCH has already landed.
* **JSON null was unusable.** Every type rule said "or null" but tested `jsonb_typeof(...) <> 'string'`,
  which is true for JSON null, so `after: null` was rejected for every path; clearing a field was
  impossible. `submit()` normalises JSON null to SQL NULL before the rules, which also makes
  `no_change` compare two nulls correctly. `synonyms` is `NOT NULL`, so clearing it writes `{}`.

D19 closes the other direction: the importer copies snapshot values over the master row, so a
snapshot exported before an apply would silently revert it. `scripts/import_joineryflow.py` now
refuses to import when any `applied` request has `applied_at > snapshot.exported_at`, naming the
ids and the first `source_id`, and writes nothing.

Verification on a fresh PostgreSQL 16 database (postgres:16 in Docker, 127.0.0.1:5432):

* `scripts/writeback_contract.sql` printed `writeback contract OK` with the new section 13: the
  happy path's `999.5` is in `material`, `rev` moved, the same diff returns `no_change`, a stale
  settle and a superseded-material settle leave the row untouched, and `after: null` writes NULL.
* `tests/test_writeback.py` 22/22 with `ML_WRITEBACK_DSN`, including the new case where the
  importer refuses the committed snapshot after an apply (`predates 1 applied write-back request`)
  and the material count and value are unchanged.
* Full suite `179 passed, 18 skipped, 200 subtests` on a rebuilt `api`-shaped database;
  `api_contract.sql` and `merge_contract.sql` green in CI order before it.
* `check_english.py`, `compileall`, `check_mappings.py`, `check_joineryflow_mapping.py` and the 93
  static unit tests are clean; the JSONL byte contract is untouched.

## Follow-up 3: retention and the upstream-write watermark

Follow-up 2 closed the loop with D19 - an import whose snapshot predates an `applied` request is
refused - but that guard read `material_change_request`. As soon as retention prunes those rows the
guard stops firing, silently: an old snapshot would import cleanly and overwrite a confirmed write.
The guard and the retention policy were one decision, so this follow-up makes both (design 6.3,
D20).

`material_writeback_state` holds one row per target system with `last_upstream_write_at`,
`last_request_id` and an `advances` counter. `settle()` advances it on **any 2xx**, including all
three `applied_unverified` branches (stale base, superseded material, local write failed), because
the PATCH answered 2xx before the local row was refused - the upstream may have taken the change.
A 4xx and an `unknown` outcome do not advance it; the 5xx / timeout case was left to the
`--verify` pass (now design 6.4), because a monotonic watermark on a transient 503 would
permanently refuse every older snapshot.

`scripts/import_joineryflow.py` now compares `snapshot.exported_at` with the watermark. Request ids
and `source_id` remain in the error message when the rows exist, but are no longer the criterion.
The `etl` CI job loads no `writeback.sql`, so the watermark table is absent there: the importer
checks `to_regclass` and falls back to the old `applied`-row query, which keeps that job green.
`scripts/prune_change_requests.py` is dry-run by default, takes `--older-than DAYS` (default 90),
deletes only `applied` and `rejected`, and never touches `material_event` or the watermark.

Verification on a fresh PostgreSQL 16 database:

* `scripts/writeback_contract.sql` section 14 asserts exactly four 2xx settles advance the
  watermark (the happy path, the stale 2xx, the tombstone 2xx and the null clear), that the
  pending -> applied refusal, the 400 and the 503 do not, and that deleting all terminal request
  rows leaves the watermark intact.
* `tests/test_writeback.py` 25/25 with `ML_WRITEBACK_DSN`, including: prune via the CLI with
  `--older-than 0 --apply`, then confirm the importer **still** refuses the committed snapshot
  and the master value is unchanged; the prune dry-run writing nothing and keeping a pending row;
  and a future `exported_at` passing the guarded comparison.
* Full suite `179 passed, 21 skipped, 200 subtests`; `api_contract.sql`, `merge_contract.sql` and
  `etl_contract.sql` green on rebuilt databases; the ETL path still imports twice
  (`18 new` then `0 new / 18 updated`).
* `check_english.py`, `compileall`, `check_mappings.py` x3, `check_joineryflow_mapping.py` and the
  93 static unit tests clean; JSONL byte contract untouched.

## Follow-up 4: `--verify`, settling a write nobody could see

Follow-up 3 left one state with no way out. `failed / uncertain_outcome` (a 5xx or timeout) is
refused by `--retry`, and the three `applied_unverified` branches (stale base, superseded
material, local write failed) had no transition at all. The only tool was a human opening the
joineryflow UI. This follow-up adds the missing read (design 6.4, D21).

The split is the same as `--apply`: Python performs the HTTP call and supplies a **verdict**; SQL
owns every transition. `material_change_request_verify_plan(id)` is pure and returns the GET URL
plus `expected` - the flat upstream column names mapped from the request's diffs through
`material_writeback_field`, with `attributes.<key>` passed through as `<key>`. It refuses a
request that is not a candidate with `not_candidate`, so no GET is spent on a terminal or pending
row.

`material_change_request_verify(id, verdict, actor, resolve)` accepts three verdicts:

* `upstream_matches` - every expected column present and equal. With `--resolve` the request
  becomes `applied`, the master row takes the diffed values, the watermark advances and exactly
  one event is written.
* `upstream_differs` - every expected column present, at least one different. The request stays
  `failed` with `error = 'verified_not_applied'`, which `--retry` accepts, because the outcome is
  now known rather than uncertain.
* `unreachable` - non-2xx, unparseable body, **or any expected column missing**. Nothing changes,
  with or without `--resolve`.

The missing-column rule is the load-bearing one. We still do not know joineryflow's read shape, so
a field name in the adapter may be wrong; reading "column absent" as "the upstream did not take
the change" would downgrade real writes to failures and invite a duplicate PATCH.

Two local-write helpers were extracted so `settle()` and `verify()` cannot drift:
`ml_writeback_apply_diffs(uid, diffs)` writes only the diffed paths, and
`ml_writeback_note_write(target, id)` advances the watermark. `settle()` now calls both, which is
a pure refactor of Follow-up 3 behaviour - the contract still counts exactly four 2xx advances.

`scripts/apply_change_requests.py` gained `--verify N` and `--resolve`. `--verify` is read-only:
zero request writes, zero events, no watermark move, asserted by the contract. It refuses to
combine with `--apply` or `--dry-run`, so a command that reads as offline can never make a live
GET, and `--resolve` without `--verify` exits 2. The fake upstream grew `do_GET` and a separate
`gets` log, so `PATCH` assertions are untouched and CI still never egresses.

Verification on a fresh PostgreSQL 16 database:

* `scripts/writeback_contract.sql` section 15 asserts: a rejected and a pending request are
  `not_candidate`; the plan returns the GET URL and `expected = {"unit_cost": 321}`; an unknown
  verdict raises `22023`; the read-only path writes zero events and leaves the watermark alone;
  `unreachable` changes nothing even with `p_resolve`; a confirmed match applies the diff, bumps
  the watermark by exactly one and writes exactly one event; a resolved request stops being a
  candidate; a mismatch records `verified_not_applied`, leaves the master row and watermark
  untouched, and can then be retried. The contract still runs twice with zero residual rows.
* `tests/test_writeback.py` 33/33 with `ML_WRITEBACK_DSN`, including: read-only `--verify`
  (one GET, no PATCH, no events, watermark untouched), `--resolve` applying a match, a mismatch
  becoming `verified_not_applied` and then retryable, a missing column changing nothing, a
  non-candidate costing no GET, a stale-base 2xx being resolved, and both CLI guards.
* Full suite `180 passed, 28 skipped, 200 subtests`; `api_contract.sql`, `merge_contract.sql` and
  `etl_contract.sql` green on rebuilt databases; the ETL path still imports twice
  (`18 new` then `0 new / 18 updated`). The one new case that is not skipped without
  `ML_WRITEBACK_DSN` is the pure verdict-rule check, which is why the passing count moved too.
* `check_english.py`, `compileall`, `check_mappings.py` x3, `check_joineryflow_mapping.py` and the
  93 static unit tests clean; JSONL byte contract untouched.

## Follow-up 5: the paths that shipped without a test

T3 was "done" with two of the seven writable fields positively asserted. `scripts/writeback_contract.sql`
proved `unit_cost` reached the master row and `product_type` could be cleared; `name`, `unit`,
`lead_time_days`, `product_url` and `synonyms` had never been written by a test, and the
`attributes.<key>` namespace had exactly one assertion - a *rejection*. The code was correct, which
I checked by hand before writing anything, but nothing would have caught it if it stopped being
correct.

Two things made this worth a follow-up rather than a note:

* `synonyms` is the only path that has to build a `text[]` instead of a scalar, and a clear has to
  produce `{}` because the column is `NOT NULL`. A break there is silent.
* Four places derive the upstream column name independently - `ml_writeback_current_values`,
  `ml_writeback_apply_diffs`, `plan()` and `verify_plan()` - each with its own
  `regexp_replace(path, '^attributes\.', '')` or `material_writeback_field` lookup. They agree
  today. Nothing said they had to.

Contract section 16 fixes both: one diff carries the five unasserted fields, the PATCH body is
compared as a whole object, and `verify_plan().expected` is asserted **equal to that body** - which
is the invariant that ties the four call sites together, since both are "the same diff, translated
the same way". `synonyms` is then cleared and must be `{}`, and `attributes.colour` is written,
checked against a pre-existing key it must not clobber, cleared, and re-submitted to prove
`no_change`.

Section 17 records the tombstone decision from design 6.4: `verify()` deliberately skips the
`material.status` re-check, so a confirmed write on a merged-away row lands **on the tombstone** and
the survivor keeps its own value. That is now asserted rather than left as an accident someone
would later "fix".

One behaviour changed: clearing an attribute now drops the key instead of storing
`{"colour": null}` (D22). The fingerprint cannot tell the difference - `attributes -> 'colour'` is
SQL NULL either way - so `no_change` and the stale check are unaffected; `attributes ? 'colour'`
just stops lying.

Verification on a fresh PostgreSQL 16 database:

* `scripts/writeback_contract.sql` green with sections 16 and 17, run twice, zero residual rows and
  no tombstone left behind.
* `tests/test_writeback.py` 37/37, run twice in a row on the same database to prove the fixture
  restores what the new cases write.
* Full suite, api / merge / etl contracts and the static checks re-run on rebuilt databases.

Two real problems surfaced while doing it, both in the test harness rather than the product:

* The `clean_db` fixture only restored `unit_cost`, `status` and `superseded_by`, so the new
  attribute and five-field cases leaked into whatever ran next. It now snapshots every column a
  write-back can touch and restores them all.
* That restore then failed with `psycopg.ProgrammingError`: psycopg does not adapt a Python `dict`
  to `jsonb` on its own, so `attributes` has to be sent as JSON text with an explicit `::jsonb`
  cast. The first run of the new fixture left rows `superseded`, which is why the second run
  failed - a reminder that a broken teardown is worse than a broken test.

## Constraints carried into implementation

* SQL owns transitions, Python owns transport (design principle 1). Satisfied: all five
  functions live in `schema/writeback.sql`; the CLI and API only map outcomes.
* Only a human approves; no auto-approval, no self-approval, no `pending -> applied` shortcut.
  Satisfied and asserted by `submit` / `decide` / `settle` and the SQL contract.
* Dry-run is the default and a true no-op. Satisfied: `tests/test_writeback.py` asserts zero
  request writes, zero events and zero HTTP calls.
* CI never egresses to joineryflow. Satisfied: the fake upstream is a local `http.server`, the
  `writeback` job sets no `ML_JF_*` variable, and `--apply` refuses to start without
  `ML_JF_BASE_URL`.
* No database lock crosses a network call; claim and settle are separate short transactions.
  Satisfied by the worker's autocommit connection.
* Never apply a stale approval, never blindly retry an uncertain outcome. Satisfied at both
  `decide()` and `settle()`, plus `retry()` refusing `uncertain_outcome`.
* Additive schema only; the five-state CHECK is untouched. Satisfied and asserted.
* One allowlist, one translation table. Satisfied: `mapping/joineryflow.yaml` is the source of
  truth and `scripts/check_joineryflow_mapping.py` keeps the SQL projections aligned.
* Any implementation PR keeps `pytest`, `check_english.py`, `api_contract.sql`,
  `merge_contract.sql` and the ETL contract green. Verified locally for every step; PR #15 had all
  four jobs green before the `writeback` job existed.

## Open

Nothing blocking. All five delivery steps of design section 14 are implemented and verified:
steps 0-1 are on `master` through PR #15, and steps 2-5 (including this log, README status and
`docs/data-model-v2.md` 5/6) are delivered in PR #16. `check_english.py` is clean, the full suite
is green, and the write-back SQL contract runs in CI on every subsequent PR.

Follow-up 1 (T3 x T2 consistency, design 6.1) is implemented and verified on a fresh PostgreSQL 16
database: the SQL contract is green on both load orders, `tests/test_writeback.py` reports 21/21
with `ML_WRITEBACK_DSN`, and the full suite reports `179 passed, 17 skipped`. The README counts
were refreshed in the same change.

Follow-up 2 (T3 x T3, design 6.2, D18/D19) is also implemented and verified: `settle('applied')`
writes the diff into `material`, `submit()` rejects values the master row cannot store, JSON null
clears a field, and the importer refuses a snapshot that predates an applied request. Counts are
now 22/22 write-back tests and `179 passed, 18 skipped`.

Two items were deliberately left open and recorded in design section 16: who resolves
`applied_unverified` and who schedules the worker and the prune CLI. The retention *policy* is no
longer open: D20 fixed what may be deleted and made the snapshot guard independent of those rows.

Follow-up 3 (design 6.3, D20) is implemented and verified: every 2xx advances the upstream-write
watermark, the importer judges staleness against it, and the prune CLI is a dry-run-by-default
operation that cannot touch the watermark or the audit trail. Counts were then 25/25 write-back
tests and `179 passed, 21 skipped`.

Follow-up 4 (design 6.4, D21) closes the last open item: `--verify` GETs the upstream row,
compares the diffed fields and reports `upstream_matches` / `upstream_differs` / `unreachable`;
`--resolve` lets SQL act on it. A missing column is `unreachable`, never a mismatch, so an
unknown read shape cannot downgrade a real write. Counts were then 33/33 write-back tests.

Follow-up 5 (design 7, D22) closes the coverage gap that followed: every allowed path is now
written by a test at least once, the four places that derive an upstream column name are pinned to
each other, and the tombstone decision is asserted instead of implied. Counts are now 37/37
write-back tests.

Still open for the project as a whole, and deliberately out of scope for T3: whether joineryflow
honours `Idempotency-Key` (until confirmed, no retry depends on it), the exact form of the
workspace guard, whether the upstream accepts a partial PATCH body, the exact `GET` response shape
and a read-scoped token, who schedules the worker and the retention job, live incremental sync
(which needs an `updated_at` column and real credentials), ETIM/eCl@ss real-code mapping and EPD
collection.
See `docs/t3-writeback.md` section 16 and the non-goals in section 13.
