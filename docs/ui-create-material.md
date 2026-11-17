# Adding new materials from the UI - design and implementation

Status: steps 1-3 of 6 implemented (`schema/create-material.sql`, `scripts/create_material_contract.sql`,
the `--apply-creates` worker pass, retained create-request rows and five HTTP routes); steps 4-6 remain proposals. It answers one question raised after the seven-step manual edit UI
shipped: the screen can find and change existing materials, but it cannot add a new one.

## 1. What exists now

| Need | Current support | Where |
| --- | --- | --- |
| View stored materials | Yes: search, facets, structured filters, paging, detail | `/ui` Edit tab, `/v1/materials`, `GET /v1/materials/{uid}` |
| Change existing values | Yes: staged diffs, one batch call, approval, audit | `/ui` staging grid, `POST /v1/change-requests/batch`, T3 |
| Approve / reject changes | Yes: queue, badges, single and bulk decisions | `/ui` Review tab, `decide()` and `decide-batch` |
| Add a new material | **No** | Only `scripts/import_joineryflow.py` inserts rows, from the committed snapshot |

The gap is real and narrow: there is no `POST /v1/materials` and no `material_create()` SQL
function. `material_change_request_submit()` also computes `before` and `base.values` from a live
row, so a request about a material that does not exist yet has nothing to compare against.

## 2. Why create is not just another diff

Two hard constraints in the existing model decide the design:

1. `material_change_request.material_uid` has `REFERENCES material(material_uid) ON DELETE CASCADE`.
   A create request cannot point at a row that has not been inserted, and adding a nullable column
   or relaxing that FK would weaken every existing request.
2. The five-state machine (`pending -> approved -> applied`, with `rejected` and `failed`) and the
   `base` fingerprint are built around "a diff against a known row". A new row has no `before`, no
   `rev` to fingerprint, and nothing to go stale against.

`material.status` already has a `draft` value in its CHECK constraint, unused today. That is useful
for a *visibility* state, but it is not an approval state: a draft row is still a real row with a
UID, a revision and events.

## 3. Options considered

### A. Reuse `material_change_request` for creates - rejected

Would need a nullable `material_uid`, a nullable `base`, and a new op discriminator on every row.
That touches every existing assertion and makes "stale base" meaningless for half the table.

### B. Insert a `draft` row immediately, publish later - rejected for this request

Smallest change: the UI inserts the row with `status='draft'`, and a later action flips it to
`active`. But the person who typed the data also publishes it, so there is no second pair of eyes
and no audit reason - exactly what the UI's edit path deliberately avoids. It remains a good
option if a "save draft" mode is wanted later.

### C. A parallel `material_create_request` table and state machine - **recommended**

A create request carries the proposed row as JSON. Approval is decided by a different actor, and a
worker inserts the material in the same transaction that moves the request to `applied`. Existing
change requests are untouched; the two flows share the actor rules, the reason rules and the audit
shape without sharing a table.

This was the choice made for this proposal.

## 4. Recommended design

### 4.1 New SQL surface

```
material_create_request(
    id             bigserial primary key,
    proposed       jsonb not null,        -- the full new material, validated at submit
    reason         text,
    status         text not null default 'pending'
                   check (status in ('pending','approved','rejected','applied','failed')),
    material_uid   text,                  -- filled only when applied; nullable until then
    requested_by / requested_at / decided_by / decided_at / applied_at / error
)
```

- `material_create_request_submit(proposed jsonb, reason text, actor text, target text)` returns
  `{ok, code, id}` and **rejects at submit** anything the master row would reject: unknown
  `category_code`, unknown `unit` code, a `name` that trims to nothing, an oversized string, a
  malformed `attributes` object, or a `material_uid` that is already taken. The same
  `material_writeback_field` allowlist and the U19/U20 checks live here, so the API is not a
  second source of truth.
- `material_create_request_update(id, proposed, reason, actor)` rewrites `proposed` and `reason`
  **only while `status = 'pending'`**, re-running every submit-time validation, and writes one
  `material_event(entity='material_create_request', op='update')` with the before/after JSON. The
  requester may edit their own pending request; a different actor may not, so approval cannot be
  pre-empted by someone else's edit. Once decided the row is immutable - a change after approval
  would invalidate the decision that was just recorded. To change an approved or rejected request,
  file a new one.
- `material_create_request_decide(id, decision, reason, actor)` mirrors `decide()`:
  different actor, reason recorded in `material_event`, no self-approval.
- `ml_material_create_apply_diffs()` inserts the row with `status='active'`, `rev=1`,
  `sync_seq` from the default sequence, `created_by`/`updated_by` from the actor, and one
  `material_event(entity='material', op='insert')`. The material UID is generated server-side
  with the existing Crockford base-32 rule; the client never supplies one.
- Before inserting, apply checks `source_system + source_id` for an existing row. If one exists
  it moves the request to `failed` with `error = 'duplicate_source: <uid>'` and writes the failure
  event; it never skips silently and never overwrites the imported row.
- The worker in `scripts/apply_change_requests.py` gains a `--apply-creates` pass. It must be
  idempotent: a request already `applied` with a `material_uid` is a no-op, not a duplicate insert.
- Retention extends to the new table with the same 90-day rule, and the audit event stays.

The proposed row is shown as a grouped field/value table by default, with the raw JSON available
in a collapsed panel for anyone who needs to check the exact payload. There is no `before` column:
the preview says "this will insert a material with these fields", not "change A to B".

### 4.2 HTTP surface

- `GET /v1/material-create-requests` and `GET /v1/material-create-requests/{id}` - the queue and
  detail, shaped like the existing change-request routes so the Review tab can reuse its components.
- `POST /v1/material-create-requests` - one create request; `status_code=201`.
- `PATCH /v1/material-create-requests/{id}` - update a **pending** request; `409 conflict` once
  decided or when the actor is not the requester, `400 bad_request` on a validation failure.
  The response is the updated row, not a new id: an edit does not create a second request.
- `POST /v1/material-create-requests/{id}/approve` and `/reject` - same error mapping as T3.
- `POST /v1/material-create-requests/batch` - optional, only if "add 20 materials" is a real
  workflow. Defer until asked; the existing batch caps (100 submit / 25 decide) were chosen for
  edits and may not suit creates.
- `GET /v1/writeback/schema` already returns the six field hints and the nine catalogs. The
  create form reuses it; the five choices that were made for editing (lead time is not a material
  attribute, notes is a textarea, unit is a select over the `units` domain) apply unchanged.

### 4.3 Form and field exposure

The choice here was to expose **all** fields. The practical grouping, so the form stays usable:

| Group | Fields | Source |
| --- | --- | --- |
| Required | `category_code`, `name` | `material_category` rows; the schema's `textarea` hint for name |
| Classification | `product_type`, `brand_id`, `default_supplier_id`, `model_no`, `family_id` | existing parties / families; `unit` select from `schema.units` |
| Commercial | `unit`, `unit_cost`, `currency`, `cost_basis` | defaults from the table (`AUD`, `ex_gst`); cost is optional |
| Text | `description`, `product_url`, `image_url`, `synonyms`, `tags` | same editors as the grid, blank stages `null` |
| Attributes | per-category keys from `catalogs[].attributes` | schema endpoint; validates type against the key's editor |

`material_uid`, `rev`, `sync_seq`, `status`, `source_*`, `search_tsv`, timestamps and actor columns
are **server-owned** and never shown as inputs. `status` starts `active` once approved; a `draft`
state remains available for a future "save without publishing" action.

### 4.4 UI shape

A third tab, `Add`, next to `Edit` and `Review`:

1. A category picker, then the full field form driven by `/v1/writeback/schema`.
2. A live preview grouped by field (Required / Classification / Commercial / Text / Attributes),
   with a collapsed "raw JSON" panel underneath. The same editors drive both the form and the
   preview, so what is shown is what will be inserted.
3. `Submit for approval` writes one create request and shows its id and `pending`.
4. A pending request can be reopened from the `Add` tab by its author and edited in place. The
   form is prefilled from `proposed`, and every field is revalidated with the same rules as a new
   submission. The header shows `pending - editable until a decision`; a decided request opens
   read-only with a link to file a new one.
5. Approved rows appear in the Review tab under a `creates` chip, with the same badges adapted:
   `own` still applies; `stale` and `inactive` do not, because there is no base row yet.
6. Once applied, the material appears in normal search. The create request keeps its `material_uid`
   so a reader can follow the audit trail from request to row.

## 5. Delivery steps and acceptance

| Step | Work | Done when |
| --- | --- | --- |
| 1 | `material_create_request` table, submit / update / decide SQL functions, event writes | `scripts/create_material_contract.sql` green with zero residual rows: field allowlist, U19/U20, client-supplied uid refused, self-approval, one event per transition, and update refused after a decision or by a non-requester |
| 2 | Apply function and worker pass; retention extension | `scripts/create_material_contract.sql` and `tests/test_create_material.py` (3/3): `approved -> applied` with one material row and one insert event (the table trigger owns it), re-running apply is a no-op, a duplicate `source_id` goes to `failed` with `duplicate_source`, and prune clears the request row while keeping the material and its events |
| 3 | HTTP routes and error mapping | `tests/test_create_material.py` 7/7: 201 submit, queued list with cursor paging, detail with event-backed `decision_reason`, `PATCH` in place (200 same id, 403 for a non-author, 409 once decided), approve/reject with 409 self-approval, 400 on validation; no token in any response |
| 4 | `Add` tab, schema-driven form, grouped preview and collapsed JSON | A form round trip submits one request with the exact proposed JSON; no client supplies a uid; the grouped view and the raw JSON show the same values |
| 5 | Review tab `creates` chip, badge adjustment and pending in-place edit | An approved create is visible, `own` is enforced, a pending request reopens prefilled and saves without changing its id, and an applied create links to the material |
| 6 | Docs and full verification | `check_english.py`, all contracts, both DSN suites, API and UI test counts updated |

Nothing in these steps changes `material_change_request` or the existing edit grid. The two flows
share the audit event table and the actor rules, not their state machine.

## 6. Decisions made before step 1

These were asked and answered before any code was written. They are recorded here so the choices do
not have to be relitigated while implementing.

1. **Preview: a grouped field table, with the raw JSON collapsed.** The table is what a reviewer
   reads; the JSON is there when someone needs to check the exact payload. Both render from the
   same `proposed` object.
2. **A pending create request is editable in place.** The author may rewrite `proposed` and the
   reason through `PATCH /v1/material-create-requests/{id}`, with all submit-time validation re-run
   and an `update` event written. It keeps its id. Once a decision exists the row is immutable; to
   change it, reject and file a new request. This differs from the edit path, where a staged diff is
   local until submit and a filed request is immutable - here the reason is that a create form has
   many fields and a single typo should not cost the requester their place in the queue.
3. **Every authenticated actor may submit a create.** The same rule as staging edits. The second
   pair of eyes comes from the approval, not from a creation whitelist.
4. **A duplicate `source_id` at apply time is a `duplicate_source` failure.** The request goes to
   `failed` with that code and the conflicting uid in `error`; it is never silently skipped and it
   never overwrites the row the ETL just wrote. A human decides whether to reject the create or
   retire the imported row.

## 7. Non-goals

- No direct `INSERT` from the browser. Every new row is a request, approved by another actor.
- No changes to supplier import or the committed joineryflow snapshot.
- No batch create until a real workflow asks for it.
- No `draft`-and-publish flow in this proposal; the field is left free for a later decision.
