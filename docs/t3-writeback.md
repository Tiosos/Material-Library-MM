# T3 - Approval-based write-back executor design

Status: design draft (step 0). Nothing is implemented yet. This file is the design record;
step 5 of the delivery plan adds `docs/t3-progress.md` with verification.
Owner: Tiosos.
Scope decision: submit a diff, review and approve or reject it, then let a worker apply the
approved diff to joineryflow. SQL owns the state machine, a CLI worker owns the HTTP hop, and
CI verifies the whole path against a fake upstream.
Companion docs: [docs/data-model-v2.md](data-model-v2.md) (section 6, T3 row),
[docs/design-v1.md](design-v1.md) (section 4.3), [docs/api-v1.md](api-v1.md) (sections 0-2),
[docs/t2-merge.md](t2-merge.md) (D7, D8), [docs/etl-joineryflow.md](etl-joineryflow.md)
(sections 5 and 12), [docs/mcp-server.md](mcp-server.md) (section 4.4).

---

## 0. One paragraph

`material_change_request` has existed since v2 and stores `diffs`, a five-state `status`, a
reason and an approver, but nothing submits a request, nothing decides one, and nothing applies
one. T3 closes that gap without changing the direction of the system: Material Library stays the
single source of truth, joineryflow is patched only after a human approves, and the ETL stays
snapshot-first. The executor is deliberately the last thing in the chain - it is the first
component in this repository that writes to an external system, so the design spends more words
on what must never happen (a real PATCH from CI, a write from a stale approval, a blind retry
after an uncertain outcome) than on the happy path.

---

## 1. Why now

1. It is the only structural gap left. `README.md` -> Current status has one unchecked item,
   [docs/data-model-v2.md](data-model-v2.md) section 6 calls T3 "the only structural gap in the
   model layer", the Open section of
   [docs/etl-joineryflow-progress.md](etl-joineryflow-progress.md) lists it first, and
   [docs/mcp-server.md](mcp-server.md) section 4.4 says "Write-back / change requests (T3) |
   Not designed yet".
2. The contract is already written. [../mapping/joineryflow.yaml](../mapping/joineryflow.yaml)
   section 6 declares `PATCH /catalog/{slug}/{mid}`, a seven-field allowlist, an attribute
   flatten-back rule and three forbidden writes. [docs/design-v1.md](design-v1.md) section 4.3
   explains why attribute keys keep their source names: to make write-back mechanical.
3. The upstream locator exists in every imported row. The ETL stores
   `material.source_id = '<table>:<pk>'` (for example `board_materials:1`), and the mapping
   declares one `endpoint` per table, so a PATCH target can be derived without a schema change.
   Verified against the 18 imported rows in the local PostgreSQL 16 database.
4. T2 established the write-side conventions. `schema/merge.sql` keeps write semantics out of
   `api.sql`, locks rows `FOR UPDATE`, sets `ml.actor` / `ml.source`, and writes explicit
   `material_event` rows. `api/main.py` already has POST routes behind `require_token`,
   `actor_from`, `dry_run` plans and the T1 error shape. T3 copies all of it.
5. The read side is stable. ETL steps 0-5 are merged and the `etl` CI job rebuilds the imported
   catalogue on every PR, so a write-back test can start from real `source_id` values instead of
   invented ones.

Deliberately **not** in T3: automatic approval, automatic retries, live incremental sync, a
`material_conflict` table, MCP write tools, multi-workspace routing, rolling back a change that
was already applied upstream, and any edit to the snapshot-first ETL contract. See section 13.

---

## 2. What exists today

| Piece | State | Where |
| --- | --- | --- |
| `material_change_request` | `id`, `material_uid` FK, `target_system`, `diffs jsonb`, `reason`, `status DEFAULT 'pending'` with `CHECK (status IN ('pending','approved','rejected','applied','failed'))`, `requested_by/at`, `decided_by/at`, `applied_at`, `error` | [../schema/material.sql](../schema/material.sql):294-309 |
| Writers | only one: `schema/sample_demo.sql:190-196` inserts a pending diff `unit_cost 42.00 -> 43.50` | [../schema/sample_demo.sql](../schema/sample_demo.sql) |
| Readers | none. No Python, SQL function, route or test references the table | repository-wide search |
| Audit | `material_revision` (one snapshot per meaningful parent change), `material_event` (`entity`, `op`, `payload`, `actor`) written by `ml_material_after()` and `ml_child_touch()`; actor from `current_setting('ml.actor')` | [../schema/material.sql](../schema/material.sql):269-289, :449-499 |
| Write semantics pattern | `material_merge()` locks both parent rows `FOR UPDATE`, sets `ml.actor='...'` and `ml.source='merge'`, and inserts explicit `material_event` rows | [../schema/merge.sql](../schema/merge.sql):586-650, :774-785 |
| Read API | FastAPI, `SCHEMA_VERSION = "2"`, one error shape `{"error":{"code","message","detail"?}}`, `require_token`, `actor_from`, `statement_timeout = '3s'` | [../api/main.py](../api/main.py):31, :109-160, :167-184; [../api/db.py](../api/db.py) |
| Upstream contract | `write_back.endpoint`, `write_back.fields`, `write_back.attributes`, three `write_back.forbidden` rules, `guarded_by: "catalog:write + workspace check"` | [../mapping/joineryflow.yaml](../mapping/joineryflow.yaml):422-437 |
| Per-table endpoint and pk | nine `tables.<name>.endpoint` values (`/catalog/board-materials`, `/catalog/equipment-hire`, ...); `pk: material_id` except `equipment_hire.pk: hire_id` | [../mapping/joineryflow.yaml](../mapping/joineryflow.yaml):58-59, :243-244 |
| Workspace | `source.workspace: hartwood-joinery`; the snapshot header carries `workspace_slug` | [../mapping/joineryflow.yaml](../mapping/joineryflow.yaml):41; [../fixtures/joineryflow-hartwood.jsonl](../fixtures/joineryflow-hartwood.jsonl) header line |
| Imported locator | `source_id = '<table>:<pk>'`, `source_payload` = full row minus `image_blob`, `keep_source_names: true` | [../mapping/joineryflow.yaml](../mapping/joineryflow.yaml):326-332; [../ingest/joineryflow.py](../ingest/joineryflow.py):379-400 |
| CI | four jobs: `static`, `database`, `api`, `etl`; `etl` uses its own fresh `ml_etl` database and `ML_ETL_DSN` | [../.github/workflows/ci.yml](../.github/workflows/ci.yml):16, :43, :174, :283 |

So T3 adds: request columns, SQL functions, a worker CLI, HTTP review routes, a SQL contract,
pytest coverage and one CI job.

---

## 3. Design principles

1. **SQL owns the state machine, Python owns the transport.** Every legal transition is a
   `plpgsql` function in `schema/writeback.sql`. The CLI resolves a target, sends one PATCH and
   reports the outcome; it never decides whether a request may move to `applied`.
2. **A human is the only thing that approves.** No auto-approval, no approval by the same actor
   who submitted in v1, and no code path that turns `pending` into `applied` directly.
3. **Dry-run is the default and is a true no-op.** `python scripts/apply_change_requests.py
   --dsn ... --dry-run` makes zero writes to `material_change_request`, zero `material_event`
   rows and zero HTTP calls. A real PATCH requires an explicit `--apply` plus configuration.
4. **CI never egresses to joineryflow.** Tests inject a fake adapter (a stdlib `http.server` on
   127.0.0.1, ephemeral port). No CI job is given `ML_JF_TOKEN`, and no test may reach a real
   upstream. This is the write-side counterpart of the ETL's snapshot-first rule.
5. **No database lock is held across a network call.** Claiming a batch is one short
   transaction; the HTTP call happens after commit; settling the outcome is a second short
   transaction. A five-minute lease reclaims work abandoned by a crashed worker.
6. **Never apply a stale approval, never blindly retry an uncertain outcome.** A request is
   bound to the field values it was filed against. A 5xx or a timeout is recorded as an unknown
   outcome for human adjudication, not retried into a duplicate write.
7. **Additive schema only.** `material_change_request` gains columns via `ALTER TABLE ... ADD
   COLUMN IF NOT EXISTS` in `schema/writeback.sql`. The five-state CHECK in
   `schema/material.sql` is not edited, and the ETL's JSONL byte contract is untouched.
8. **One allowlist, one translation table.** A diff path is either one of the seven UMR fields
   named by `write_back.fields` or `attributes.<source column>`. Anything else is rejected at
   submit time and named in the error.

---

## 4. Preflight findings and the decisions they force

These three came out of reading the shipped code, not from the design prose. Each is resolved
here so step 1 does not have to guess.

### 4.1 CI must not connect to the source system

The ETL is snapshot-first by design ([docs/etl-joineryflow.md](etl-joineryflow.md) section 12):
CI never connects to joineryflow and a byte-identical re-export is the acceptance criterion. T3
is the first component with an outbound write. **Decision:** the executor takes a client object;
the production client is a thin `urllib` PATCH, the CI client is a fake adapter started inside
the pytest process. `--apply` without `ML_JF_BASE_URL` is a configuration error (exit 2), not a
fallback to a live endpoint. The `writeback` CI job sets only `ML_WRITEBACK_DSN`.

### 4.2 The URL slug is per-table, not per-workspace

A first reading of `PATCH /catalog/{slug}/{mid}` suggests `{slug}` is `workspace_slug`, and the
workspace slug is indeed not persisted in the database: `source_payload` stores the row columns
only, so zero of the 18 imported rows contain `workspace_slug`. That reading is wrong. Each of
the nine tables declares its own `endpoint` in the mapping, so `{slug}` is the catalog slug
(`board-materials`, `equipment-hire`, ...) and `{mid}` is the numeric part of `source_id`.

**Decision:** no new locator column, no ETL change. Resolve the target from
`tables.<name>.endpoint` plus `source_id`, both of which are already available. The workspace is
needed only for `guarded_by: "catalog:write + workspace check"`, so it is configuration:
`ML_JF_WORKSPACE`, defaulting to `source.workspace` in
[../mapping/joineryflow.yaml](../mapping/joineryflow.yaml) (`hartwood-joinery`). The ETL header
check already refuses a snapshot from another workspace, and UMR v2 has no namespace column, so
v1 stays single-workspace and sends the workspace as a guarded header. A mismatch between the
configured workspace and the snapshot header is a hard error at executor startup.

### 4.3 The demo's `unit_cost` diff is legitimate

`schema/sample_demo.sql` files a pending diff changing `unit_cost` from `42.00` to `43.50`,
while `write_back.forbidden` says "Do not change prices already snapshotted on a quote". Read
together with `write_back.fields`, which explicitly maps `unit_cost`, the two rules are about
different objects: **updating the catalogue's own `unit_cost` is allowed; retroactively changing
a price already copied onto a quote is forbidden.** The UMR has no quote rows, no quote table
and no path that reaches one, so the allowlist enforces the rule structurally.

**Decision:** keep the demo diff as the fixture for the happy path. Do not add a special-case
"price" check that would reject a legal catalogue update and force somebody to disable it. The
contract asserts instead that no PATCH body ever contains `archived_at`, `status`, `material_uid`
or `source_*` keys.

---

## 5. Data model: `schema/writeback.sql`

New file, loaded after `schema/merge.sql` **and after** `schema/seed.sql` in the `api` job, and
after `schema/seed.sql` in the new `writeback` job. Additive only, mirroring T2's D8.

Implementation note (step 4): this sentence originally said "before seed.sql", which is wrong.
`material_writeback_catalog.category_code` references `material_category`, whose rows
`seed.sql` inserts; loading this file first fails on that foreign key. Both jobs now load
`seed.sql` before `writeback.sql`.

```sql
ALTER TABLE material_change_request
    ADD COLUMN IF NOT EXISTS base             jsonb,       -- field values at submit time
    ADD COLUMN IF NOT EXISTS attempts         integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS claimed_at       timestamptz,
    ADD COLUMN IF NOT EXISTS claimed_by       text,
    ADD COLUMN IF NOT EXISTS idempotency_key  text,
    ADD COLUMN IF NOT EXISTS last_status      integer;

CREATE UNIQUE INDEX IF NOT EXISTS material_change_request_key_idx
    ON material_change_request (idempotency_key);
CREATE INDEX IF NOT EXISTS material_change_request_claim_idx
    ON material_change_request (status, claimed_at, requested_at);
```

`status` keeps its five values. `claimed_at` is the in-flight marker: `approved` plus
`claimed_at IS NULL` means claimable. There is deliberately no `claimed` status, because adding
one would require editing the CHECK in `schema/material.sql`.

| Function | Returns | Writes |
| --- | --- | --- |
| `material_change_request_submit(p_uid, p_diffs, p_reason, p_actor, p_target)` | `jsonb` | one row, `base`, one `material_event` |
| `material_change_request_decide(p_id, p_decision, p_reason, p_actor)` | `jsonb` | status, `decided_by/at`, one event |
| `material_change_request_claim(p_limit, p_actor, p_lease)` | `setof`/`jsonb` batch | `claimed_at/by`, `attempts + 1`, `idempotency_key` |
| `material_change_request_plan(p_id)` | `jsonb` | none (pure, used by dry-run and `GET`) |
| `material_change_request_settle(p_id, p_outcome, p_status_code, p_error, p_actor)` | `jsonb` | status, `applied_at`, `error`, `last_status`, clears claim, the master row for `applied`, one event |
| `material_change_request_queue(p_status, p_limit, p_cursor)` | `jsonb` | none |

`base` is `{ "rev": <material.rev at submit>, "values": { "<path>": <live value> } }`. It is
field-level on purpose: step 4 of the ETL measured that a no-op re-import bumps `sync_seq` on 11
rows through `ml_child_touch()` while `rev` stays 1, so a whole-row or `sync_seq` fingerprint
would produce false staleness.

Every transition writes one `material_event` with `entity = 'material_change_request'`,
`op = 'insert'` on submit and `'update'` afterwards, and
`payload = {"request_id", "from", "to", "actor", "reason"}` (submit also carries the diff paths
and decide the decision verb). `actor` comes from `X-Actor` on the HTTP edge or `--actor` on the
CLI, via `set_config('ml.actor', ..., true)`; `ml.source` is `'writeback'`. The request table has
no triggers today, so these inserts are explicit, exactly as `material_merge()` does it.

Implementation note (found during step 1 verification): `jsonb_set` returns SQL NULL when its
`new_value` argument is SQL NULL, so `submit()` normalises a missing value to JSON null before
building `base.values`, and `ml_writeback_current_values` does the same through
`jsonb_object_agg` (which converts SQL NULL values to JSON null). The two representations must
stay byte-equal for the stale check, so a missing attribute key reads as `"path": null` in both.

---

## 6. State machine

```
pending --approve--> approved --claim--> approved (claimed) --settle(applied)--> applied
   |                     |                        |
   +--reject--> rejected +--stale--> failed        +--settle(failed)--> failed
                                                        |
                                    failed --retry (explicit, human)--> approved
```

| From | To | Guard |
| --- | --- | --- |
| (new) | `pending` | uid exists, `status` not archived/superseded, `target_system='joineryflow'`, every path allowed, `diffs` non-empty, `base.values` computed server-side |
| `pending` | `approved` | `decided_by <> requested_by` in v1; the material is still `active` (not archived, not merged away); `base` still matches; no other open request for the same uid + path |
| `pending` | `rejected` | `decided_by` set, reason optional (T2 D10); a tombstone may still be rejected, which is how a queue is cleared |
| `approved` | `applied` | only from `settle()` after a 2xx, only if the row is still claimed by this worker, only if the material is still `active`, and the master row is updated in the same transaction (section 6.2) |
| `approved` | `failed` | stale base, invalid target, material archived or merged away, 4xx, 5xx, timeout or connection error |
| `failed` | `approved` | explicit `--retry N` with a new actor; only when `outcome` is known-failed, never `unknown`; material still `active` |
| `rejected`, `applied` | any | terminal; 409 `conflict` |

`material_change_request_decide()` re-checks `base` and returns `{"ok": false, "code":
"stale_base"}`, which the HTTP edge maps to 409. `settle()` re-checks it again immediately before
writing `applied`; if it changed, the request becomes `failed` with `error = 'stale_base'` even
if the upstream already answered 2xx. That case is surfaced in the report as
`outcome = 'applied_unverified'` and needs human attention - it is the one place where the two
systems can legitimately diverge.

### 6.1 T3 x T2: a merge and an in-flight request cannot both be true

T2 and T3 meet at `material_uid`, and the meeting was unguarded. `material_merge()` supersedes the
loser instead of deleting it (T2 D4: a tombstone keeps old links resolving), and that tombstone
**keeps its field values**. T3's approval fingerprint is field-level, so a merge that
lands after `submit()` changes no value the fingerprint looks at: `decide()` would approve,
`plan()` would build a PATCH, and the executor would write to an upstream row another human just
declared a duplicate. `submit()` refuses a tombstone, but only at submit time.

Neither obvious repair is acceptable. **Re-pointing** an open request at the keeper changes
`{mid}`, which is derived from `source_id`, so the approval would silently apply to a different
upstream record than the one the approver saw. **Auto-failing** the requests inside
`material_merge()` adds a `pending -> failed` edge to the five-state machine, which is exactly
what D13 keeps out of `schema/material.sql`.

So the rule is split between the two modules, and every piece of it is enforced in SQL:

| # | Where | Guard | Result |
| --- | --- | --- | --- |
| 1 | `material_merge_preview()` (T2) | a `pending` or `approved` request on **either** side of the pair | blocker `open_change_request`, `blocking_requests: [id, ...]`, so `material_merge()` returns 409 `conflict` |
| 2 | `material_change_request_decide()` | `approve` on a material whose `status` is `archived` or `superseded` | `{ok:false, code:'material_inactive', material_status}` -> 409 |
| 3 | `material_change_request_plan()` | same status check, before the target is resolved | `{ok:false, code:'material_inactive'}`; `--apply` turns a non-ok plan into `settle('failed', error='material_inactive')` **before** any HTTP call |
| 4 | `material_change_request_settle()` and `retry()` | the same check again | a 2xx that landed on a row merged away mid-flight is `failed / material_inactive` and reported as `applied_unverified`; `retry()` refuses it |

The merge blocker is the policy; guards 2-4 are defence in depth for the race the blocker cannot
cover (a merge that commits between two of the worker's short transactions) and for rows that were
superseded before this rule existed.

Four consequences worth stating:

- **`claim()` is deliberately not a guard.** Skipping a tombstone there would leave an `approved`
  row that no worker ever touches and no human ever sees. Claiming it and letting `plan()` refuse
  ends it as `failed / material_inactive`, which is visible in the review queue.
- **`reject()` still works on a tombstone.** Clearing a queue must not require reviving the row;
  rejecting stale data is always safe.
- **The blocker is symmetric.** A request on the keeper blocks the merge too: the merge rewrites
  keeper fields from the plan, so it can invalidate the very approval that is in flight.
- **A blocked merge has one exit.** Decide the request - reject it, or approve and apply it - then
  merge. `blocking_requests` names the ids, so the operator does not have to search the queue.

The guard lives in the status column, not in the fingerprint, so the field-level `base` design is
untouched: a no-op re-import still cannot make an approval stale.

### 6.2 T3 x T3: a confirmed write lands in the master record

The first implementation of `settle()` recorded `applied` and stopped there: it never wrote the
approved values into `material`. Measured on a fresh database, a request moving `unit_cost` from
`3450` to `3460` ended `applied` while the master row still read `3450` - and the same diff could
be filed again immediately, because `submit()` computes `before` from that stale local value and
`open_request` only looks at `pending` and `approved`.

That contradicts section 0 ("Material Library stays the single source of truth") and leaves
[design-v1](design-v1.md) section 10 item 2 - the master-data ownership decision - answered in
prose but not in code. The two coherent options were:

- **ML is the source of truth** - `settle()` writes the diff home. The request table records the
  approval; the master row is the data.
- **ML is a mirror** - the next snapshot brings the value back, and the same diff must be refused
  in the meantime. That keeps the ETL as the only writer, but makes every read between the PATCH
  and the next export wrong, and weakens `base` to a local-only comparison.

The first is chosen (D18). `settle('applied')` therefore:

1. re-checks the field-level `base` and the material status, as before;
2. applies **only the diffed paths** to `material` in the same transaction - field paths through
   `material_writeback_field`, `attributes.<key>` through `jsonb_set`, `synonyms` as a text array,
   and JSON null as SQL NULL (clearing a field);
3. lets the existing triggers turn that into one `material_revision` row and one `material_event`
   with `ml.source = 'writeback'`;
4. records `applied` afterwards. A local write that fails (constraint, unknown unit, ...) does not
   lose the 2xx: the request becomes `failed / local_write_failed` and is reported as
   `applied_unverified`, exactly like a stale base.

Three consequences:

- **The same diff cannot be filed twice.** After a successful apply, `submit()` sees
  `before = after` and returns `no_change`. This is what makes a second `--apply` safe for a
  different reason than before: previously the request was terminal, now the data agrees.
- **The window closes for readers.** `GET /v1/materials/{uid}`, export and search show the new
  value as soon as the worker commits, instead of waiting for the next snapshot.
- **`submit()` validates what the master row can store**, because the value will be written there:
  `unit` must be a known unit code (it is a foreign key), `unit_cost` must fit `numeric(14,4)`, and
  `lead_time_days` must fit `integer`. Rejecting them at submit keeps the write from failing after
  a PATCH has already landed.

**JSON null was previously unusable.** Every type rule said "or null" but compared
`jsonb_typeof(...) <> 'string'`, which is true for JSON null, so `after: null` was rejected for
every path - clearing a field was impossible. `submit()` now normalises JSON null to SQL NULL
before the type rules, which also makes `no_change` compare two nulls correctly.

**The ETL must not undo it.** The importer copies snapshot values over the master row, so a
snapshot taken *before* an apply would silently revert it. `scripts/import_joineryflow.py` now
refuses to import when any `applied` request has `applied_at > snapshot.exported_at`, and names the
offending ids and the first `source_id` (D19). The check reads the request table, so it depends on
`applied` rows being retained; retention is still an operations decision (section 16).

Implementation note (step 2): `claim()` and `settle()` keep the five states and use `claimed_at`
as the in-flight marker. A claim writes no event row, so a happy path emits exactly three events
(insert, approve, apply) as acceptance criterion 2 requires; `settle()` writes the third one.
Both functions take a `p_lease` parameter defaulting to five minutes, which lets the contract
tests exercise expiry without a real wait. `settle()` refuses a caller that no longer owns a live
lease, and `retry()` is the explicit human `failed -> approved` transition that refuses
`error = 'uncertain_outcome'`. `claim()` also takes an optional `p_only_id` so the CLI's
`--request-id` can claim exactly one request without Python filtering or releasing a batch.

---

## 7. The diff contract

`diffs` is a JSON array of `{ "path", "before", "after" }`, matching
[docs/data-model-v2.md](data-model-v2.md) section 1. The client submits only `path` and `after`;
**`before` is always computed server-side** from the live UMR row, so a caller cannot forge it.
An empty array, a duplicate path, or `before = after` is a 400 `bad_request`. A JSON null `after`
means "clear this field": it is normalised to SQL NULL, written home as NULL (or `{}` for
`synonyms`, which is `NOT NULL`), and a repeated null is a `no_change`.

Allowed paths:

| UMR path | Upstream column | Source |
| --- | --- | --- |
| `name` | `description` | `write_back.fields` |
| `product_type` | `product_type` | `write_back.fields` |
| `unit` | `um` | `write_back.fields` |
| `unit_cost` | `unit_cost` | `write_back.fields` |
| `lead_time_days` | `default_lead_time_days` | `write_back.fields` |
| `product_url` | `product_website` | `write_back.fields` |
| `synonyms` | `synonyms` | `write_back.fields` |
| `attributes.<column>` | the same column | `write_back.attributes` + `keep_source_names: true` |

The mapping writes `fields` as `<upstream column>: <UMR field>`, so the executor uses the
**inverse**. The `<column>` in `attributes.<column>` must appear in that table's `attributes`
list in the mapping; that list is the second half of the allowlist and is validated by the
extended `scripts/check_joineryflow_mapping.py` in step 0.

Rejected, with the offending path named in the error: `archived_at`, `status`, `material_uid`,
`source_id`, `source_payload`, `source_system`, `rev`, `sync_seq`, any `price.*`, `measure.*`,
`identifier.*`, `quote*`, and any unknown or unlisted path. Rejection happens at submit, so a
forbidden value can never reach the executor. The SQL contract asserts that no PATCH body in the
fake upstream's log contains a forbidden key.

---

## 8. Target resolution and the PATCH request

Given a request row:

1. Read `material.source_id`, split once on `:` into `table` and `mid`. Reject anything that is
   not `<table>:<integer>`; `equipment_hire` uses `hire_id` but the UMR form is identical.
2. Look up `tables.<table>.endpoint` in [../mapping/joineryflow.yaml](../mapping/joineryflow.yaml)
   (`/catalog/board-materials` for `board_materials`). Reject if the table is unknown, or if
   `material.source_system <> 'joineryflow'`.
3. `slug` is the last path segment of that endpoint, `mid` is the numeric id. Note that the
   workspace is **not** part of the URL (section 4.2).
4. Build the body from the inverse allowlist in section 7, translating UMR paths to upstream
   columns. `attributes.<column>` flattens to the same-named column of that table.
5. Send `PATCH {base_url}/catalog/{slug}/{mid}` with
   `Content-Type: application/json`, `Authorization: Bearer <ML_JF_TOKEN>`,
   `Idempotency-Key: mlcr-{id}`, and `X-Workspace-Slug: <ML_JF_WORKSPACE>` (the workspace
   guard). Timeout `ML_JF_TIMEOUT`, default 5s.

`material_change_request_plan(p_id)` returns exactly `{ "method", "url", "headers" (token
redacted), "body", "idempotency_key", "base" }`. The dry-run report and `GET
/v1/change-requests/{id}` return this plan without sending anything. The plan is byte-for-byte
what `--apply` sends, the same "preview is the contract" rule as T2 section 3.

`Idempotency-Key` is sent but not relied upon: joineryflow's support for it is an open question
(section 16). Until confirmed, a failed request is never retried automatically.

---

## 9. Executor: `scripts/apply_change_requests.py`

```
python scripts/apply_change_requests.py --dsn ... [--dry-run | --apply]
       [--limit 100] [--request-id N | --retry N] [--actor NAME] [--json out.json]
```

- `--dry-run` is the default and makes **zero writes** (section 3): it selects claimable rows,
  builds plans and reports them. It does not claim, settle or emit events.
- `--apply` requires `ML_JF_BASE_URL`; missing configuration exits 2 before any database write.
- `ML_JF_TOKEN` (required by the upstream guard), `ML_JF_WORKSPACE` (default from the mapping),
  `ML_JF_TIMEOUT` (5s), and `ML_JF_ACTOR` (default `executor`) are the only new settings. The
  token is never printed, never written to the JSON report and never logged.
- Claim: `SELECT ... WHERE status='approved' AND (claimed_at IS NULL OR claimed_at < now() -
  interval '5 minutes') ORDER BY requested_at FOR UPDATE SKIP LOCKED LIMIT p_limit`, then commit.
- For each claimed row: build the plan; a plan that refuses (`invalid_target`, `material_inactive`)
  settles `failed` with that code and sends nothing; on a 2xx call `settle('applied')`; on a 4xx call
  `settle('failed', error=upstream message)`; on 5xx, timeout or connection error call
  `settle('unknown', error='uncertain_outcome')` and record `last_status` when there is one.
  There is **no automatic retry** and no second PATCH for the same request in one run.
- Report: `{"ok", "mode", "checked", "claimed", "applied", "failed", "unknown", "skipped",
  "rows":[{"id","status","url","http_status","error"}]}`. Exit 0 when nothing failed, 1 when any
  request ended `failed` or `unknown`, 2 on a configuration error.
- A second `--apply` over an already-applied database reports `applied = 0` and sends no HTTP.
  Re-running is safe by construction: `applied` and `rejected` are terminal, and `failed` needs
  an explicit `--retry`.

The HTTP call happens outside any open transaction (section 3, principle 5). A worker that dies
mid-flight leaves `claimed_at` set; a later worker reclaims it after the five-minute lease and
must first `GET` the upstream record before deciding - this is documented, not automated, in v1.

---

## 10. HTTP edge (`api/main.py`)

All routes use the existing `require_token`, `actor_from`, `SCHEMA_VERSION = "2"` and the T1
error shape. No route calls joineryflow; the executor is a worker, not a request handler, so the
API never holds a response open for an upstream timeout.

| Route | Behaviour |
| --- | --- |
| `POST /v1/change-requests` | body `{material_uid, diffs:[{path, after}], reason?, target_system?}`; 201 `{id, status:'pending', base, schema_version}`; 400 `bad_request` (unknown uid, forbidden or unknown path, empty diffs, bad target), 409 `conflict` (archived/superseded material, open request for the same uid + path) |
| `GET /v1/change-requests` | `status=pending\|approved\|rejected\|applied\|failed\|all` (default `pending` + `approved` + `failed`), `limit` 1..100 default 20, `cursor`; `{schema_version, total, rows[], next_cursor}` |
| `GET /v1/change-requests/{id}` | full request plus `material_change_request_plan()` with the token redacted; 404 when unknown; no writes |
| `POST /v1/change-requests/{id}/approve` | body `{reason?}`; 200 report; 404 unknown; 409 `conflict` (already decided, stale base, decided_by = requested_by) |
| `POST /v1/change-requests/{id}/reject` | body `{reason?}`; 200 report; 404 / 409 as above |

There is intentionally no `POST /v1/change-requests/{id}/apply` (D1). `api/requirements.txt` is
unchanged: `urllib` from the standard library is enough for one PATCH per request, and the
worker does not need FastAPI or httpx.

---

## 11. Tests, SQL contract and CI

- `scripts/writeback_contract.sql`: psql-runnable, `\set ON_ERROR_STOP on` plus `DO $$ ...
  RAISE EXCEPTION ... $$` assertions, the same shape as
  [../scripts/etl_contract.sql](../scripts/etl_contract.sql). Asserts the additive columns,
  every legal transition, the illegality of `pending -> applied`, `base` staleness, the allowlist
  (including the demo `unit_cost` diff being accepted while `archived_at` is rejected), the
  `Idempotency-Key` being unique, one `material_event` per transition, and the T3 x T2 guards of
  section 6.1 (merge blocker, decide, plan, settle, retry), plus section 6.2: an applied write is
  in the master row, `rev` moved, the same diff returns `no_change`, and `after: null` clears a
  field.
- `tests/test_writeback.py`: pure unit cases for path translation and target resolution run
  without a database; integration cases skip unless `ML_WRITEBACK_DSN` is set, exactly as
  `tests/test_etl.py` skips without `ML_ETL_DSN`. The fake upstream is a stdlib `http.server`
  started by a pytest fixture on 127.0.0.1 with an ephemeral port; it records method, path,
  headers and body. Cases: dry-run is a no-op, happy path, 400 / 409 / 500 / timeout, stale
  approval, concurrent claim (two workers, one PATCH per request), forbidden paths, a merge
  blocked by an open request, a request whose material was merged away (approve refused,
  zero PATCHes), the master row taking the value, and the importer refusing a stale snapshot
  after an apply.
- New CI job `writeback` with its own fresh `POSTGRES_DB=ml_writeback` and
  `ML_WRITEBACK_DSN`. Steps: install `pyyaml`, `psycopg[binary]`, `pytest`; load
  `material.sql, search.sql, merge.sql, seed.sql, writeback.sql` (merge.sql is needed by the
  T3 x T2 assertions of section 6.1); import the committed snapshot with
  [../scripts/import_joineryflow.py](../scripts/import_joineryflow.py) so real `source_id`
  values exist; `psql -f scripts/writeback_contract.sql`; `pytest -q tests/test_writeback.py
  --noconftest`. No `ML_JF_*` credential is set in CI.
- The `api` job adds `schema/writeback.sql` to its load order so the file is exercised there
  too. Its `pytest -q tests` will collect the new integration cases as skipped. `static`
  already covers the new `.md`, `.sql` and `.py` files through `check_english.py` and
  `compileall`.

---

## 12. Acceptance criteria

1. `--dry-run` on a database with one pending and one approved request writes 0 rows to
   `material_change_request`, emits 0 `material_event` rows and makes 0 HTTP calls; the JSON
   report contains the exact URL, headers and body that `--apply` would send.
2. submit -> approve -> `--apply` against the fake upstream ends at `applied`, with
   `applied_at` set, `attempts = 1`, `last_status = 200` and exactly three event rows
   (insert, approve, apply). The fake sees exactly one PATCH with the inverse-translated body.
3. Changing a diffed field between submit and approve returns 409 `conflict`; changing it
   between approve and apply ends the request as `failed` with `error = 'stale_base'`, and the
   report says `applied_unverified` when the upstream already answered 2xx.
4. Every forbidden path is rejected at submit with the path named in the error; no recorded
   PATCH body contains `archived_at`, `status`, `material_uid` or `source_*`.
5. Two executors started concurrently against the same approved batch produce one PATCH per
   request (`FOR UPDATE SKIP LOCKED`), not two.
6. A 5xx or timeout ends as `failed` with `error = 'uncertain_outcome'`, records `last_status`
   when available, and produces no automatic retry or second PATCH. `--retry` refuses an
   unknown outcome.
7. A second `--apply` over an all-applied database reports `applied = 0` and sends 0 requests.
8. CI is green on all five jobs (`static`, `database`, `api`, `etl`, `writeback`);
   `check_english.py` is clean; the full suite still passes and the ETL SQL contract is
   unchanged. The JSONL byte contract stays `i/lf w/lf attr/-text`, 15733 bytes, 0 CRLF.
9. Docs: README Current status gets a T3 line, [docs/data-model-v2.md](data-model-v2.md)
   section 6 moves T3 to Done, and `docs/t3-progress.md` records per-step verification.
10. A material superseded by a merge can be neither approved, planned, applied nor retried:
    `decide` returns 409 `material_inactive`, `plan` returns the same code, `--apply` sends zero
    PATCHes and ends the request as `failed / material_inactive`, and a merge is refused with the
    blocker `open_change_request` while a `pending` or `approved` request names either side of the
    pair (section 6.1).
11. A confirmed write is in the master record: after `--apply` the diffed fields hold the `after`
    values, `rev` has moved, the same diff returns `no_change`, and `after: null` clears a field
    (section 6.2). An import of a snapshot whose `exported_at` predates an applied request fails
    with the offending ids named and writes nothing.

---

## 13. Risks, caveats and non-goals

| Risk | Mitigation |
| --- | --- |
| A real PATCH leaks into CI | fake adapter only; `--apply` refuses to start without `ML_JF_BASE_URL`; no credential in any workflow |
| Upstream columns drift | the allowlist and the endpoint map both live in `mapping/joineryflow.yaml`; the extended mapping validator checks them on every PR |
| A stale approval overwrites newer master data | field-level `base`, checked at approve and again at apply (section 6) |
| A 5xx is actually a successful write | no automatic retry; `unknown` outcome needs a human `GET` before any retry |
| A crashed worker blocks a request forever | five-minute lease plus `claimed_at IS NULL OR claimed_at < ...` |
| Two workers patch the same row | claim with `FOR UPDATE SKIP LOCKED`, stable `Idempotency-Key` per request |
| A long HTTP call pins a row | claim and settle are separate short transactions; no lock crosses the network |
| Two workspaces are mixed | v1 is single-workspace; the ETL header check refuses a foreign workspace and the executor compares its configured workspace with the snapshot header |
| A quote price is changed retroactively | the UMR has no quote rows; the allowlist cannot express a path to one (section 4.3) |
| The token leaks into a report or log | redacted in every plan, never written to JSON, never logged; CI has no token |

Non-goals: automatic approval, automatic retries, a `material_conflict` table (it is mentioned
only in the mapping's `sync` section and does not exist in the schema), live incremental sync or
a `PUT /v1/materials/{uid}`, soft-archiving via write-back (archiving is a joineryflow business
action), rolling back an applied change, MCP write tools, multi-workspace routing, ETIM/eCl@ss
and EPD collection, changes to search ranking, and any change to the ETL snapshot or its
`import_contract.expected` numbers. Merge (T2) continues to bypass
`material_change_request` (T2 D7).

---

## 14. Delivery plan

| Step | Work | Done when |
| --- | --- | --- |
| 0 | This design document; extend `scripts/check_joineryflow_mapping.py` to assert that the `write_back` allowlist, the nine endpoints and the per-table `attributes` lists agree with each other | `check_english.py` clean, validator green, no runtime code added, no schema change |
| 1 | `schema/writeback.sql`: additive columns, `submit()`, `decide()`, `plan()`, `queue()`; contract assertions 1-3 | state machine is psql-assertable on a fresh database |
| 2 | `claim()` and `settle()`, lease, event rows, concurrent-claim assertion | two sessions claim disjoint batches; one event per transition |
| 3 | `scripts/apply_change_requests.py` plus the fake adapter and `tests/test_writeback.py` | dry-run no-op, happy path, 4xx/5xx/timeout and stale cases pass locally |
| 4 | HTTP review routes, `scripts/writeback_contract.sql`, new `writeback` CI job | five CI jobs green; full pytest green |
| 5 | Docs: README status, `data-model-v2.md` section 6, `docs/t3-progress.md` | `check_english.py` clean, relative links intact, PR opened |
| 6 | T3 x T2 consistency (section 6.1): merge blocker, status guards at decide / plan / settle / retry, contract assertions and integration cases | contract green on both CI load orders, 21/21 write-back tests, full suite green |
| 7 | T3 x T3 loop (section 6.2): `settle()` writes the diff home, submit validates storable values, JSON null clears, the importer refuses a stale snapshot | contract green, 22/22 write-back tests, full suite green, an old snapshot is refused after an apply |

Effort: about two to three days at the T1/T2/ETL pace; most of it is the state machine, the
outcome handling and making the CI simulation honest.

---

## 15. Decisions recorded

| Id | Decision | Rationale |
| --- | --- | --- |
| D1 | No HTTP `apply` route; the executor is a CLI worker | a network write does not belong in a request/response cycle; keeps the API's 3s statement timeout irrelevant to upstream latency |
| D2 | `--dry-run` is the default and a true no-op; `--apply` is explicit | the first outbound write in this repository should be impossible to trigger by accident |
| D3 | CI uses a fake adapter and never egresses to joineryflow | preserves the ETL's snapshot-first boundary on the write side |
| D4 | SQL owns transitions, Python owns transport | the T1/T2 rule; CLI and API cannot drift |
| D5 | `{slug}` comes from `tables.<name>.endpoint`, `{mid}` from `source_id` | both already exist; no new locator column and no ETL change |
| D6 | Workspace comes from `ML_JF_WORKSPACE` / `source.workspace`, not the database | UMR v2 has no namespace column; the ETL header check already enforces a single workspace |
| D7 | `before` is computed server-side; `base` is field-level, not `sync_seq` | prevents forged diffs and false staleness from child-table touches |
| D8 | Stale base is 409 at approve and `failed` at apply | never apply an approval to data it was not filed against |
| D9 | `unit_cost` is allowed; the quote rule is enforced by the allowlist | the demo diff is a catalogue update, not a quote edit; a special price check would be wrong |
| D10 | No automatic retries; a 5xx or timeout is an `unknown` outcome for a human | a retry after an uncertain PATCH can duplicate a real write |
| D11 | Claim and settle are separate short transactions | no database lock is held across an HTTP call |
| D12 | Five-minute lease plus `FOR UPDATE SKIP LOCKED` | recovers from a crashed worker without double-patching |
| D13 | Additive columns only; the five-state CHECK is untouched | mirrors T2, keeps `schema/material.sql` stable for the other four CI jobs |
| D14 | Integration tests skip unless `ML_WRITEBACK_DSN` is set | the `api` job's databases are not prepared for write-back; same pattern as `test_etl.py` |
| D15 | `Idempotency-Key` is sent but never relied on | upstream support is unconfirmed; behaviour must be correct without it |
| D16 | T2 merge still bypasses `material_change_request` | T2 D7: that table models cross-system approvals, not internal master-data maintenance |
| D17 | A merge is blocked by an open request on either side; T3 re-checks `material.status` at decide, plan, settle and retry | a tombstone keeps its field values, so the base fingerprint cannot see a merge; re-pointing a request would change the upstream record its approver saw, and auto-failing it inside merge would add a sixth transition (section 6.1) |
| D18 | `settle('applied')` writes the diff into `material` in the same transaction | the library is the source of truth (section 0); without it every read stays stale until the next snapshot, `base` compares local-to-local, and the same diff can be filed and PATCHed twice (section 6.2) |
| D19 | An import whose snapshot predates an applied request is a hard error | the importer copies source values over the master row, so an old snapshot silently reverts a confirmed write; re-exporting is cheap, losing an applied change is not |

---

## 16. Open questions

1. Does joineryflow honour `Idempotency-Key`? Until it is confirmed, D15 stands and no retry
   depends on it.
2. What is the exact workspace guard - a `catalog:write` token scope plus a header name, or a
   path-level check? The adapter isolates this; only the header name is a guess today.
3. Should `attributes.<column>` flatten-back ship in v1, or only the seven common fields? The
   design allows both; the delivery plan starts with the seven and adds attribute cases once the
   first real diff needs one.
4. Does the upstream accept a partial PATCH body, or does it require the full row? A full-row
   body would be riskier and would need a `GET` before each PATCH; this is deliberately out of
   v1.
5. Retention for `applied` and `rejected` requests, and who runs the worker on a schedule, are
   operations decisions. Like the 90-day search-log retention, they are documented rather than
   scheduled in this repository. Retention now also bounds the D19 guard: purging `applied` rows
   re-opens the window in which a stale snapshot could revert a confirmed write.
6. Who resolves `applied_unverified` - a stale base, a material merged away mid-flight, or a local
   write that failed? v1 leaves all three to a human. A `--verify` pass that GETs the upstream row
   and compares it with the local one is the natural next step, but it needs the upstream's read
   shape and a token with read scope, so it is deliberately not in this round.
