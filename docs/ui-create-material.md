# Adding new materials from the UI - proposal

Status: proposal only. No code is written by this document. It answers one question raised after
the seven-step manual edit UI shipped: the screen can find and change existing materials, but it
cannot add a new one.

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
- `material_create_request_decide(id, decision, reason, actor)` mirrors `decide()`:
  different actor, reason recorded in `material_event`, no self-approval.
- `ml_material_create_apply_diffs()` inserts the row with `status='active'`, `rev=1`,
  `sync_seq` from the default sequence, `created_by`/`updated_by` from the actor, and one
  `material_event(entity='material', op='insert')`. The material UID is generated server-side
  with the existing Crockford base-32 rule; the client never supplies one.
- The worker in `scripts/apply_change_requests.py` gains a `--apply-creates` pass. It must be
  idempotent: a request already `applied` with a `material_uid` is a no-op, not a duplicate insert.
- Retention extends to the new table with the same 90-day rule, and the audit event stays.

Because the proposed row is JSON, the UI can show a real PATCH-style preview before approval:
"this will insert a material with these fields". There is no `before` column to show.

### 4.2 HTTP surface

- `GET /v1/material-create-requests` and `GET /v1/material-create-requests/{id}` - the queue and
  detail, shaped like the existing change-request routes so the Review tab can reuse its components.
- `POST /v1/material-create-requests` - one create request; `status_code=201`.
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
2. A live preview of the proposed JSON with the same before/after language the Review sheet uses
   (here there is only "after").
3. `Submit for approval` writes one create request and shows its id and `pending`.
4. Approved rows appear in the Review tab under a `creates` chip, with the same badges adapted:
   `own` still applies; `stale` and `inactive` do not, because there is no base row yet.
5. Once applied, the material appears in normal search. The create request keeps its `material_uid`
   so a reader can follow the audit trail from request to row.

## 5. Delivery steps and acceptance

| Step | Work | Done when |
| --- | --- | --- |
| 1 | `material_create_request` table, submit and decide SQL functions, event writes | Contract asserts the allowlist, the U19/U20 checks, duplicate-uid refusal, self-approval and one event per transition |
| 2 | Apply function and worker pass; retention extension | A request goes `approved -> applied`, the material row and its insert event exist, re-running apply is a no-op, and prune keeps the event |
| 3 | HTTP routes and error mapping | 201 on submit, 409 on duplicate/forbidden, per-item codes if a batch is added later |
| 4 | `Add` tab, schema-driven form and preview | A form round trip submits one request with the exact proposed JSON; no client supplies a uid |
| 5 | Review tab `creates` chip and badge adjustment | An approved create is visible, `own` is enforced, and an applied create links to the material |
| 6 | Docs and full verification | `check_english.py`, all contracts, both DSN suites, API and UI test counts updated |

Nothing in these steps changes `material_change_request` or the existing edit grid. The two flows
share the audit event table and the actor rules, not their state machine.

## 6. Open questions before step 1

1. **Does a create need a diff preview at all?** The JSON is the whole row, which may be long. A
   field-by-field table is friendlier but more code. *Leaning: show the table, keep the JSON
   collapsible.*
2. **Can a create request be edited after submission?** *Leaning: no.* Reject and submit again,
   matching the edit path's immutability.
3. **Who may create?** Every authenticated actor today can stage edits. *Leaning: same rule for
   creates*, with approval providing the second pair of eyes.
4. **What happens to a create request whose material was made by the ETL meanwhile?** The unique
   uid is server-generated, so a collision is unlikely, but a duplicate `source_id` could still
   happen. *Leaning: refuse at apply with a new `duplicate_source` code rather than silently
   skipping.*

## 7. Non-goals

- No direct `INSERT` from the browser. Every new row is a request, approved by another actor.
- No changes to supplier import or the committed joineryflow snapshot.
- No batch create until a real workflow asks for it.
- No `draft`-and-publish flow in this proposal; the field is left free for a later decision.
