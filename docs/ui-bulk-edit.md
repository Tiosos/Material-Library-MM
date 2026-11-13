# Manual query and bulk edit UI

Status: design, not implemented. Decisions are numbered `U1..U20`; T3's write-back decisions are
`D1..D22` in [t3-writeback.md](t3-writeback.md) and are not repeated here.

## 0. One paragraph

A browser screen for a person who needs to find any number of materials and correct them by hand:
search and filter to a working set, edit cells in a grid, stage every change, then submit the whole
batch as ordinary `material_change_request` rows so the existing approval path, allowlist, stale-base
check, watermark and audit events all still apply. A second tab reviews what other people filed,
singly or in bulk, with a mandatory reason that lands in the audit event and is read back from it.
The UI owns no data and writes nothing directly: it is a front end for `/v1/materials` and
`/v1/change-requests`, plus four small API additions (a schema endpoint, sort and numeric filters on
the list route, a batch submit route, and a batch decide route) and two stricter checks in `submit()`
so a name of newlines and a 100 KB paragraph are both refused before they reach a request row.

## 1. What exists, and what is missing

| Piece | State |
| --- | --- |
| Read API | `GET /v1/materials` with `q`, `category`, `brand`, `supplier`, `attrs`, `measures`, `limit` (max 100), `cursor`, `expand`. No sorting, no numeric range on `unit_cost` or `lead_time_days`, no `status` filter. |
| Write-back API | `POST /v1/change-requests` (one material per call), `GET /v1/change-requests` (queue), `GET /v1/change-requests/{id}` (detail + PATCH preview), `POST .../approve`, `POST .../reject`. All present and tested. No batch submit, no batch decide, and a decision's reason is only in `material_event`, not on the request row. |
| Executor | `scripts/apply_change_requests.py`, `--dry-run` by default, `--apply`, `--retry`, `--verify`. Unchanged by this work. |
| Prototype page | `proto/web/index.html`, read-only search against the prototype database, served by `proto/serve.py`. Not reused: different database, different server, no write path. |
| Manual editing | Nothing. Every change today is a `curl`, a script, or a CLI flag. |

The gap is not capability, it is ergonomics: the state machine already accepts exactly the operations
a person needs, one HTTP call at a time, and there is no way to see 40 rows, change 12 of them and
submit the result. Two of the gaps are real API gaps rather than UI gaps - a batch of 40 materials is
40 calls today, and "who approved this and why" needs a join to the audit trail - which is why this
design touches `api/main.py` and one column before it touches a single pixel.

## 2. Scope

### 2.1 In scope

- Query: free text, facets, structured filter rows, sorting, paging.
- Edit: the seven allowlisted fields and `attributes.<key>` for the material's own table, including
  multi-line text for `name` and `attributes.notes`.
- Stage and submit: many materials, many fields, one action, per-material results, a mandatory reason
  above 5 materials.
- Review: the actionable queue, the diff, single and bulk approve and reject with a mandatory reason
  for bulk decisions, and the decision record (who, when, why) read from the request row.
- Identity: a token and an actor, both entered once and remembered locally.

### 2.2 Out of scope

- Creating materials. `material` rows come from the importer; the write-back contract has no insert
  path and this UI does not invent one.
- Deleting, merging, archiving. Merge is T2 and has its own preview and blockers.
- Applying. `--apply` stays a worker decision (D1): no HTTP route will PATCH the upstream from a
  request handler.
- Editing `status`, `archived_at`, `rev`, `sync_seq`, `source_*`, `price.*`, `measure.*`. These are
  `forbidden_path` at submit (T3 section 7); the UI renders them read-only and never offers an editor.
- Server-side saved views, sharing, per-user permissions, multi-tenant anything.

## 3. Why not adopt NocoDB, Baserow or Directus

They solve the same visual problem and would take an afternoon to point at this database. They also
write straight to the table, which is exactly what this repository spent four PRs making impossible
for `material`.

A direct `UPDATE material` skips:

- the allowlist, so a `status` or `archived_at` edit is one keystroke away;
- the `base` fingerprint, so two people can silently overwrite each other;
- `material_change_request`, so the upstream never learns about the change and the next import
  reverts it;
- `material_event`, so there is no record of who changed what;
- the upstream-write watermark, so the importer's stale-snapshot guard has nothing to compare against.

NocoDB and Directus are good products; they are generic table editors. This is not a table editor,
it is a form in front of a five-state approval machine. The grid is the part worth copying; the write
path is the part that must not be.

## 4. Decisions

| Id | Decision | Rationale |
| --- | --- | --- |
| U1 | The UI never writes to `material`. Every edit becomes a `material_change_request`. | The allowlist, stale base, watermark and audit trail only exist on that path. A second write path would need all four reimplemented and kept in step. |
| U2 | One request per material per submit, not one per changed cell. | `material_change_request_submit()` already takes a `diffs` array. Per-cell requests would collide with each other through `open_request` on the same row. |
| U3 | Edits are staged in the browser; nothing is sent until "Review and submit". | The same rule as `--dry-run`: the preview is the contract. A person should see the whole batch before it exists. |
| U4 | Batch submit is one new route returning a result per material; partial success is normal, not an error. | Forty rows where three conflict is the expected case, not a failure. Returning 409 for the whole batch would throw away thirty-seven good requests. |
| U5 | The editable surface comes from the server, never from a hardcoded list in JavaScript. | The allowlist lives in `material_writeback_field` and `material_writeback_catalog`. A UI copy would drift the first time the mapping changes. |
| U6 | Filters are structured rows (field, operator, value), not an expression string. | No parser, no `eval`, no SQL injection surface, and the compiler is a pure function that maps rows onto existing query parameters. |
| U7 | Saved views live in `localStorage`, not in the database. | A view is one person's convenience, not shared data. Persisting it would add a table and a migration for no audit value. |
| U8 | Paging is 50 rows by default (the API caps at 100); bulk actions apply to the current page's selection only. | Matches the human task: you review what you can see. "Select all 12,000 matching" is a script, not a screen. |
| U9 | Token and actor are separate fields. | `require_token` authenticates the client; `X-Actor` is the identity `decide` compares against `requested_by`. Conflating them would make the self-approval guard either unusable or bypassable. |
| U10 | The interface is English-only. | `scripts/check_english.py` scans `.html`, `.css` and `.js`, so any CJK in the UI fails the `static` CI job. Relaxing that for `api/ui/` is a one-line change but it weakens a repository-wide invariant, so it is an owner decision, not an implementation detail. |
| U11 | No build step. Vanilla ES modules, one stylesheet, served by FastAPI's `StaticFiles`. | The repository has no `package.json` and its CI is Python. Adding npm for one screen would add a toolchain, a lockfile and a second supply chain to a project that currently has neither. |
| U12 | A stale base is surfaced before submit where the server can tell, and always after. | `base` is only checked at submit and at approve. The grid can compare the value it loaded with the live value on a refresh, and must show the `stale_base` conflict per row rather than a generic failure. |
| U13 | `synonyms` edits as a token list; `attributes.<key>` edits as a typed scalar. | `synonyms` is `text[]` and a raw JSON textarea invites `["a", 1]`. Attribute values are string, number, boolean or null; the editor keeps the type it found. |
| U14 | No optimistic UI on submit. A row shows "submitting" then the server's answer. | The answer decides whether the row is pending, `no_change`, `open_request` or `stale_base`. Guessing it means showing a state that may not exist. |
| U15 | A batch submit of more than 5 materials requires a reason, and the server enforces it. | Five is the point where a person stops remembering why; below it the diff itself is the explanation. Enforcing it on the server rather than in the form means the CLI and any future client inherit the rule. |
| U16 | Bulk approve is a batch decide route. A reason is required, and every request keeps its own `decided_at` from the database clock. | Approving twenty rows with no recorded why is the failure mode this whole repository is built to avoid. Per-request timestamps are truthful even inside one batch; they differ by milliseconds and that is fine. |
| U17 | `name` and `attributes.notes` are textareas that preserve line breaks. The editor hint comes from the schema endpoint, not from a length heuristic. | A length heuristic would render a short name as a single-line input, so a person could not type the first line break. Presentation belongs with the allowlist: one server answer decides both. |
| U18 | No `decision_reason` column. The decision reason is read from `material_event` through a `LATERAL` projection in `queue()` and in the detail route. | The event already carries `reason`, `actor` and `at` from `decide()`, so a column would be a second copy of one fact. It would also be the copy that disappears: `scripts/prune_change_requests.py` deletes `applied` and `rejected` rows after 90 days but never touches `material_event`, so the event is the record that outlives the request. One lateral join over 20 queue rows is cheaper than a denormalised column that can drift. |
| U19 | `name`, `product_type` and `unit` are trimmed of spaces and newlines and refused when the result is empty. `product_url` is exempt: a material may legitimately have no URL. | `btrim()` trims spaces only, so `"\n\n"` currently passes as a non-empty name, and `product_type` has no emptiness check at all. That hole was unreachable while the editors were single-line; a textarea makes it a keystroke away. `product_url` is different in kind: "no page for this item" is a real answer, so a blank URL clears the field instead of being refused. All three columns are nullable, so `null` stays a legal value for every one of them - clearing is an operation, not an error. |
| U20 | Any string value in a diff is capped at 1000 characters, enforced in `submit()` as SQLSTATE `22023`. The cap is per value, not per field. | A pasted 100 KB `notes` would otherwise travel through the request row, the event payload and the PATCH body, and the upstream would be the first thing to complain. `22023` already maps to `400` at the HTTP edge, so the cap needs no new error plumbing. Per value means a `synonyms` array of fifty short tokens is fine; only one runaway string is refused. |

## 5. Architecture

```
browser (api/ui/)
  index.html        shell, two tabs: Edit / Review
  app.js            query state, grid, staging, submit, review
  styles.css        one file, CSS variables, light theme
        |
        | fetch, Authorization: Bearer <token>, X-Actor: <actor>
        v
api/main.py
  GET  /ui                     StaticFiles(api/ui, html=True)      (new)
  GET  /v1/writeback/schema    fields + catalogs + editor hints     (new)
  GET  /v1/materials           + sort, + status, + cost/lead range  (extended)
  POST /v1/change-requests/batch      per-material results, reason rule   (new)
  POST /v1/change-requests/decide-batch  per-request results, reason      (new)
  ... existing read and write-back routes, unchanged
        |
        v
schema/writeback.sql   submit / decide / claim / settle / retry / verify
                       + submit(): name/product_type/unit trim includes newlines,
                         product_url exempt, string cap 1000 per value
                       + queue(): projects decision_reason from material_event
                       (no new column)                          (extended)
```

Nothing in the diagram is a new state machine. The four route additions are a projection, two loops
and two `WHERE` clauses; the schema change is two checks inside `submit()` and one `LATERAL` join in
`queue()`. No column is added, so no migration and no second copy of a fact that already exists in
`material_event`.

## 6. Query layer

### 6.1 The filter model

The UI holds one query object:

```json
{
  "q": "blum hinge",
  "facets": {"category": "hardware", "brand": "Blum", "supplier": "Blum"},
  "filters": [
    {"field": "unit_cost", "op": "gte", "value": 100},
    {"field": "status", "op": "eq", "value": "active"},
    {"field": "attributes.colour", "op": "eq", "value": "Black"}
  ],
  "sort": {"field": "unit_cost", "order": "desc"},
  "page": {"limit": 50, "cursor": null}
}
```

`compileQuery(query)` is a pure function returning a query string for `/v1/materials`:

- `q` becomes `q`.
- `facets.category`, `facets.brand`, `facets.supplier` become `category`, `brand`, `supplier`.
- `attributes.<key>` equality filters merge into one `attrs` JSON object.
- `measures.<key>` comparisons merge into one `measures` JSON object (`{gte, lte, unit}`).
- `unit_cost` and `lead_time_days` comparisons become `unit_cost_min` / `unit_cost_max` /
  `lead_time_min` / `lead_time_max`.
- `status` equality becomes `status`.
- `sort` becomes `sort` + `order`.
- `page` becomes `limit` + `cursor`.

A field that is not in the whitelist is dropped with a visible warning rather than sent, so a typo
narrows the result set instead of silently widening it.

### 6.2 API additions

`GET /v1/writeback/schema` - read-only, token-protected, no writes:

```json
{
  "schema_version": "2",
  "targets": ["joineryflow"],
  "fields": [
    {"path": "name", "material_column": "name", "upstream_column": "description",
     "editor": "textarea"},
    {"path": "unit_cost", "material_column": "unit_cost", "upstream_column": "unit_cost",
     "editor": "number"}
  ],
  "catalogs": [
    {"table_name": "board_materials", "category_code": "board",
     "attributes": [{"key": "colour", "editor": "text"},
                    {"key": "notes", "editor": "textarea"}]}
  ]
}
```

`editor` is one of `text`, `textarea`, `number`, `integer`, `url`, `tokens`, `boolean`, `select`.
`name` and `notes` are `textarea`; `synonyms` is `tokens`; `unit` is `select`. The map lives in the
HTTP edge next to the projection, so a new long-text field is one entry there and no JavaScript
change (U17). The endpoint is the only place a client learns which cells are editable and how to
render them.

`GET /v1/materials` gains five parameters, all optional:

| Parameter | Type | Notes |
| --- | --- | --- |
| `status` | text | exact, one of `active`, `archived`, `superseded` |
| `unit_cost_min` / `unit_cost_max` | numeric | inclusive |
| `lead_time_min` / `lead_time_max` | integer | inclusive |
| `sort` | text | whitelist: `name`, `unit_cost`, `lead_time_days`, `updated_at`, `source_id` |
| `order` | text | `asc` or `desc`, default `asc` |

`sort` is a whitelist, not a column name interpolated from the request. The four numeric bounds are
plain comparisons; none of them is parsed from an expression.

`POST /v1/change-requests/batch` - at most 100 materials per call:

```json
{"target_system": "joineryflow",
 "reason": "Q4 supplier price list",
 "materials": [
   {"material_uid": "mat_...", "diffs": [{"path": "unit_cost", "after": 903}]},
   {"material_uid": "mat_...", "diffs": [{"path": "attributes.notes", "after": "Line one\nLine two"}],
    "reason": "drawing revision B"}
 ]}
```

A top-level `reason` applies to every item that does not carry its own, and a per-item reason wins.
**When `materials` has more than 5 entries, at least one of the two must be non-empty**; otherwise the
call is refused with `400 reason_required` before any row is written (U15). The threshold and the
check live on the server so the CLI and any future client inherit them. A reason longer than 500
characters is refused with `400 reason_too_long`, matching `submit()`'s own limit.

Response is always `200`; the per-item code carries the outcome:

```json
{"schema_version": "2",
 "submitted": 2, "failed": 1,
 "results": [
   {"material_uid": "mat_...", "ok": true, "id": 41, "status": "pending"},
   {"material_uid": "mat_...", "ok": false, "code": "open_request",
    "detail": {"path": "unit_cost"}}
 ]}
```

Each item calls `material_change_request_submit()` in its own statement, so one failure does not roll
back the others. The per-item code taxonomy is the existing one (`_SUBMIT_FAILURES`): `not_found`,
`conflict`, `forbidden_path`, `no_change`, `open_request`. `reason_required` and `reason_too_long` are
the only new codes, they are request-level (not per item), and they add no SQL.

`POST /v1/change-requests/decide-batch` - at most 100 request ids per call (U16):

```json
{"decision": "approve", "ids": [41, 42, 43], "reason": "Q4 price list, checked against the PDF"}
```

`decision` is `approve` or `reject`. `reason` is **required for both**, because a bulk decision with no
recorded why is exactly what the audit trail exists to prevent. Response is `200` with per-item
results:

```json
{"schema_version": "2", "decided": 2, "failed": 1,
 "results": [
   {"id": 41, "ok": true, "status": "approved", "decided_by": "bob",
    "decided_at": "2026-11-14T08:12:04.512Z"},
   {"id": 42, "ok": false, "code": "self_approval", "requested_by": "bob"}
 ]}
```

Each id goes through `material_change_request_decide()` unchanged, so `self_approval`, `stale_base`,
`material_inactive` and `already_decided` are reported per row and the rest of the batch still lands.
`decided_at` comes from the database clock per request; inside one batch those timestamps differ by
milliseconds, which is truthful rather than a shared fiction. The single-request routes keep their
existing optional reason, so nothing already shipped changes behaviour.

### 6.3 Saved views

`localStorage["ml.views"]` holds `[{name, query}]`. Applying a view replaces the query object and
re-runs the search. Deleting one is local. There is no server-side list, and the Review tab does not
use views.

## 7. Edit layer

### 7.1 Staging, not immediate writes

Clicking a cell opens the editor 7.2 assigns to that field. Committing it - `Enter` or blur for a
single-line input, `Ctrl+Enter` or `Apply` for a textarea - does not send anything: it records a
staged diff and marks the cell dirty with the original value kept for the before/after comparison.
`Escape` reverts the cell.

A footer bar shows `12 changes across 9 materials` with three actions: `Review`, `Discard all`,
`Reload`. `Reload` refetches the current page and drops staged diffs whose base moved, naming them.

Staging is keyed by `material_uid` and then by path, so editing the same cell twice keeps the last
value, and editing `unit_cost` and `name` on one row produces one request with two diffs (U2).

### 7.2 Field editors

| Field | Editor | Validation before staging |
| --- | --- | --- |
| `name` | textarea in a popover, 3 rows, line breaks preserved | non-empty after trimming spaces and newlines (U19); at most 1000 characters; no leading or trailing whitespace kept |
| `product_type` | text | non-empty after trimming spaces and newlines (U19) |
| `unit` | select | must be a code from the schema endpoint's `unit` domain; non-empty after trimming |
| `unit_cost` | number | finite, non-negative, max 4 decimals |
| `lead_time_days` | integer | non-negative |
| `product_url` | text | blank is allowed and stages `null` (clearing); otherwise it must start with `http://` or `https://` |
| `synonyms` | token list, comma separated | each token non-empty, no duplicates, at most 1000 characters |
| `attributes.<key>` where `editor = textarea` (today: `notes`) | textarea in a popover, 6 rows | type must stay a string; at most 1000 characters; an empty value stages `null`, not `""` |
| `attributes.<key>` otherwise | typed input | matches the JSON type of the current value; string, number, boolean or null |

Clearing is a first-class edit, not an error. An empty `product_url`, an empty `notes` or an emptied
`product_type` stages `null` and submits as `after: null`, which `submit()` accepts and
`ml_writeback_apply_diffs()` writes as SQL NULL. The UI never stages an empty string for a text field;
it stages `null`. The server still accepts `""` for `product_url` because that column has no
emptiness rule (U19), so a CLI user is not forced into one representation.

Client validation only prevents obviously wrong input; `submit()` is still the authority and its
`forbidden_path`, `conflict` and `no_change` answers are shown verbatim. The 1000-character cap is
checked in the form so a paste is refused immediately, and in `submit()` so no other client can skip
it (U20).

A textarea cell renders as a single line clipped to the column, with a `¶` marker and the line count
when the value holds a newline. Clicking it opens the popover, which is where the text is edited; the
grid never tries to grow a row to fit a paragraph. `Enter` inserts a line break inside a textarea and
`Ctrl+Enter` (or the popover's `Apply`) stages the value, because `Enter` cannot both mean "newline"
and "commit" in a long-text field. The popover shows a live `742 / 1000` counter, turns it red past
the cap, and disables `Apply` until the value fits.

Line breaks are sent to the upstream verbatim: `name` becomes `description` and `notes` becomes
`notes` on the PATCH. Whether joineryflow accepts a newline there is unknown (T3 section 16), so a
staged value containing one carries a small warning in the review sheet. It is not blocked, because
the upstream answer is the only authority and a 4xx surfaces as a failed request, not as data loss.

### 7.3 Submit

`Review` opens a sheet listing every staged diff grouped by material: name, the path, the before
value, the after value, and a reason box. One reason for the whole batch is offered and copied into
each item, because `submit()` stores it per request; a per-material box overrides it.

The reason field is mandatory once the sheet covers more than 5 materials (U15). The sheet says so
before the attempt (`6 materials: a reason is required`), and `Confirm` stays disabled until the box
is non-empty, so the server's `reason_required` is a backstop rather than the first thing a person
sees. A staged value that contains a line break is flagged in the same sheet, as described in 7.2.

On confirm the sheet sends one batch call, then replaces the sheet body with the results: submitted
rows show their request id and `pending`; failed rows show the code and stay staged so the person can
fix or drop them. Nothing is retried automatically.

## 8. Review layer

### 8.1 Queue

`GET /v1/change-requests?status=actionable` (pending + approved + failed) with paging. Each row shows
the material name, the request id, the diff count, the age, the status and the requester.

Filter chips: `pending`, `approved`, `failed`, `all`. A status filter is a query parameter, not a
client-side filter, so the queue stays correct past the first page.

### 8.2 Detail

Clicking a row opens the request detail: the diffs with `before` and `after` side by side, the
`base` values, the live values fetched from `/v1/materials/{uid}`, and the PATCH preview from
`GET /v1/change-requests/{id}` (`plan.url` and `plan.body`, with no token in it).

Three indicators matter and are shown as badges, not prose:

- **stale** - any live value differs from `base.values`. Approving will return `stale_base`.
- **inactive** - the material is `archived` or `superseded`. Approving will return `material_inactive`.
- **own** - the actor in the top bar equals `requested_by`. Approving will return `self_approval`.

### 8.3 Approve and reject

Both call the existing routes with `X-Actor` and an optional reason. A refused decision renders the
error code and its message; it never silently retries. After a successful decision the row leaves the
actionable queue on the next refresh, and the UI does not pretend the request was applied: it says
`approved`, because `--apply` is still a separate worker run.

The detail view shows the decision once it exists: `decided_by` and `decided_at` come from the
request row, and `decision_reason` comes from the audit event (U18). `material_change_request_queue()`
gains one `LATERAL` projection so the queue can show it without a second round trip:

```sql
LEFT JOIN LATERAL (
    SELECT e.payload ->> 'reason' AS decision_reason
      FROM material_event e
     WHERE e.entity = 'material_change_request'
       AND (e.payload ->> 'request_id')::bigint = r.id
       AND e.payload ->> 'to' IN ('approved', 'rejected')
     ORDER BY e.seq DESC
     LIMIT 1
) d ON true
```

The detail route uses the same lookup. No column, no trigger, and the reason stays where `decide()`
already put it. The consequence worth stating plainly: **`material_event` is now the only record of a
decision's reason**, so the existing rule that `prune_change_requests.py` never deletes events is
load-bearing rather than tidy. T3's contract already asserts it; this design depends on it.

Before a decision exists, the three badges of 8.2 stand in for the answer the server is going to give.

### 8.4 Bulk approve

Selecting rows in the queue and pressing `Approve selected` opens the same sheet pattern as submit:
the exact ids, the material names, the diff count per row, and a **required** reason. Confirm calls
`POST /v1/change-requests/decide-batch` once and replaces the sheet with per-row results.

Three rules make this safe to offer:

- The reason is required by the server, not just by the form (U16). A batch approve with no reason is
  refused with `400 reason_required` and nothing is decided.
- The sheet lists what will be approved. `Approve 20 requests` is never a single button with a count
  that a person cannot expand.
- Rows that come back `self_approval`, `stale_base`, `material_inactive` or `already_decided` stay in
  the queue with the code shown; the rest are approved. Partial success is the normal case.

Each approved row displays its own `decided_at` from the response, not the time the button was
pressed. `decision_reason` is the batch reason, so the audit trail reads "approved by bob at
08:12:04, reason: Q4 price list, checked against the PDF" on every row.

Reject uses the same route with `decision: "reject"` and the same required reason. Rejecting does not
re-check `base` - `decide()` deliberately allows rejecting stale data, since refusing a request is
always safe.

### 8.5 Actor switching

The top bar holds `token` and `actor`. The actor is sent as `X-Actor` on every write. When the actor
matches `requested_by`, the approve button is disabled with the reason `a submitter cannot approve
their own request`, and a `switch actor` shortcut focuses the field. In a bulk approve, rows filed by
the current actor are unchecked by default and marked `own`. This is a convenience, not a guard:
`decide()` refuses them anyway.

## 9. Identity and configuration

- `ML_API_TOKEN` is optional on the server (`require_token` is a no-op when unset). The UI therefore
  treats the token field as optional and shows `auth off` when `/v1/meta` answers without a 401.
- `token` and `actor` are stored in `localStorage` under `ml.token` and `ml.actor`. Neither is ever
  written into the DOM after entry, into a URL, or into an error message.
- `X-Actor` defaults to the server's `actor_default` when the field is empty, which is what the CLI
  does. The UI shows the effective actor so a batch is not filed under a surprise name.
- No cookie, no session, no CSRF surface: every call is a `fetch` with an explicit header.

## 10. Screens

```
+-------------------------------------------------------------------------------+
| Material Library   [ Edit ] [ Review ]        actor: [ alice ]  token: [ **** ]|
+---------------------+---------------------------------------------------------+
| Search [ blum     ] | 151 materials   sort [unit_cost v]  [Filters] [Views v] |
|                     | +-----------------------------------------------------+ |
| Category            | | [ ] name            | unit | unit_cost | lead | ... | |
|  board         (64) | | [x] Blum hinge 110  | ea   |  903.00   |   3  |     | |
|  hardware      (38) | | [x] Blum hinge 71B  | ea   |  412.50   |   5  |     | |
| Brand               | | [ ] Laminex 18mm ...| m2   |   88.00   |  10  |     | |
|  Blum          (22) | | [ ] Board 3 ¶ 2 lines| m2  |   95.00   |   7  |     | |
| Supplier            | +-----------------------------------------------------+ |
|  Blum          (22) |  < 1 2 3 ... >        12 changes across 9 materials         |
|                     |  [Review] [Discard all] [Reload]                         |
+---------------------+---------------------------------------------------------+
```

`¶ 2 lines` marks a value holding line breaks; the cell stays one line and the textarea opens on
click. The Review tab reuses the layout: the left column becomes the status chips, the centre becomes
the request list with a checkbox column, and the detail opens in a right-hand drawer instead of a
modal so the queue stays visible while a decision is made. Selecting rows raises the same footer bar
with `Approve selected` and `Reject selected`, both of which open the reason sheet of 8.4.

## 11. Failure taxonomy

Every failure the UI can show, and what it means for the person:

| Code | Where | What the UI says | What the person does |
| --- | --- | --- | --- |
| `no_change` | submit | the value already equals the live value | the diff is dropped from staging |
| `open_request` | submit | an open request already covers this path | open the queue, find it, decide it |
| `stale_base` | submit, approve | the material changed since it was loaded | `Reload`, then re-apply the edit |
| `conflict` | submit | the material cannot accept a change request | the row is archived or superseded; read-only |
| `forbidden_path` | submit | the path is not allowed | a UI bug; the cell should not have been editable |
| `material_inactive` | approve | the material was archived or merged away | reject the request |
| `self_approval` | approve | a submitter cannot approve their own request | switch actor |
| `already_decided` | approve | the request was already decided | refresh the queue |
| `reason_required` | submit, decide | more than 5 materials, or a bulk decision, with no reason | write one reason for the batch |
| `reason_too_long` | submit, decide | the reason is over 500 characters | shorten it |
| `value_too_long` | submit | a string value is over 1000 characters | shorten it; the editor counts characters live |
| `401 unauthorized` | any | token missing or wrong | re-enter the token |
| network error | any | the server did not answer | nothing was written; retry |

`forbidden_path` reaching a person is a UI defect by construction (U5), which is why it is listed
rather than hidden.

## 12. Tests and CI

- `tests/test_ui.py`: the shell is served at `/ui` with `text/html`; `app.js` and `styles.css` are
  served with the right content types; the shell loads without a token; `/v1/writeback/schema`
  returns the seven fields and nine catalogs and matches `material_writeback_field` row for row;
  `editor` is `textarea` for `name` and for `notes`, `tokens` for `synonyms`, `number` for
  `unit_cost`, and every attribute key the catalog allows has an editor.
- `tests/test_writeback_api.py` (extend): batch submit with two good and one `open_request` returns
  `200`, `submitted: 2`, `failed: 1`, and the failed item keeps its code; a batch of 101 materials
  is refused with 400; a batch containing a `forbidden_path` fails only that item; **5 materials with
  no reason succeed, 6 with no reason return 400 `reason_required`, 6 with a top-level reason succeed
  and each request stores it, and a per-item reason overrides the batch one**; a reason of 501
  characters returns 400 `reason_too_long`.
- `tests/test_writeback_api.py` (decide-batch): a reason is required for both decisions; three ids
  where one was filed by the same actor returns `decided: 2, failed: 1` with `self_approval` on that
  row and `decided_at` present on the two that landed; the queue and the detail route both report the
  same `decision_reason`, and it comes from the event - the request row still has no such column.
- `tests/test_api.py` (extend): `sort` accepts only the whitelist and rejects `; drop table`;
  `unit_cost_min` / `unit_cost_max` / `lead_time_min` / `lead_time_max` / `status` narrow the result
  set as expected, asserted against `scripts/api_contract.sql` data.
- `scripts/api_contract.sql` (extend): the same numeric and status predicates asserted in SQL, so the
  Python and SQL views of the data cannot drift.
- `scripts/writeback_contract.sql` (extend): `name`, `product_type` and `unit` set to newlines only are
  refused (U19), while `product_url` set to `""` is accepted and clears the field; a 1001-character
  value is refused with `22023` while 1000 is accepted, per value rather than per field (U20);
  `queue()` returns the decision reason from the event; and after deleting every `applied` and
  `rejected` row the reason is still readable, which is the property U18 depends on.
- `scripts/check_english.py` already covers the new files; no change.
- No new CI job. The `api` job runs all of it.

The JavaScript has no unit test harness and will not get one (U11). Its logic that matters - the
query compiler and the staging reducer - is written as pure functions in `app.js` so a future
decision to test it is a small step, not a rewrite.

## 13. Acceptance criteria

1. `/ui` serves the shell without a token, and with `ML_API_TOKEN` set every data call without a
   token returns 401 and the UI shows the token prompt instead of an empty table.
2. Searching `blum` and filtering `category = hardware` and `unit_cost >= 400` returns the same rows
   as the equivalent `/v1/materials` call, and the filter chips reflect the compiled query.
3. Editing `unit_cost` on three rows and `name` on one row stages four diffs across four materials;
   `Review` lists them grouped by material with before and after.
4. Submitting that batch creates exactly four `pending` requests, one per material, each carrying its
   own diffs; the database shows four rows and four `material_event` inserts.
5. Two edits on the same row submit as one request with two diffs, not two requests.
6. A batch where one material already has an open request returns `submitted: 3, failed: 1`, the
   three are `pending`, the fourth shows `open_request` and stays staged.
7. A value equal to the live value returns `no_change` and the diff is dropped rather than retried.
8. The Review tab lists `actionable` requests; approving one with a different actor moves it to
   `approved` and writes one event; approving with the same actor is refused with `self_approval` and
   the button is disabled before the click.
9. A request whose material was superseded after filing shows the `inactive` badge, and approving it
   returns `material_inactive` rather than a generic error.
10. `GET /v1/writeback/schema` matches `material_writeback_field` exactly: seven fields, and the
    attribute keys per catalog equal `material_writeback_catalog.attribute_keys`. `name` and `notes`
    carry `editor: "textarea"`, `synonyms` carries `tokens`, `unit_cost` carries `number`.
11. A textarea cell preserves line breaks: staging `Line one\nLine two` for `name` submits that exact
    string, the request detail shows two lines, and a name of newlines only is refused (U19). The same
    refusal applies to `product_type` and `unit`; a blank `product_url` is accepted and clears the
    field instead of failing.
12. A 1000-character value is accepted and a 1001-character one is refused with `400` naming the path;
    the textarea counter shows `1001 / 1000` and `Apply` is disabled before the attempt (U20).
13. Five staged materials submit with no reason; six are refused by the form until a reason is typed,
    and the same six sent directly to the API return `400 reason_required` with zero rows written.
14. Bulk approving three requests with a reason returns `decided: 3`; each row afterwards shows its
    own `decided_at`, and the same reason is reported by the queue and the detail route even though
    the request table has no column for it (U18).
15. A bulk approve with no reason returns `400 reason_required` and decides nothing.
16. A bulk approve where one request was filed by the current actor returns `decided: 2, failed: 1`,
    the two are `approved`, the third stays `pending` with `self_approval` shown, and the sheet does
    not close on failure.
17. No file under `api/ui/` contains CJK; `check_english.py` stays green. No `package.json`, no
    `node_modules`, no build step in any CI job.
18. The five existing CI jobs stay green, and the full suite still passes with the new tests counted.

## 14. Risks and non-goals

| Risk | Mitigation |
| --- | --- |
| The UI becomes a second write path by accident | It has no SQL and no direct connection; the only mutating calls are the four write-back routes (U1). A test asserts the batch routes call `material_change_request_submit` and `material_change_request_decide` and nothing else. |
| A batch of 100 submissions blocks a worker for a while | Each item is one short statement; 100 items is 100 inserts, not 100 HTTP calls. The cap is enforced server-side. |
| A person stages 40 edits, leaves the tab, and loses them | Staged diffs are mirrored to `sessionStorage` and restored on reload with a banner naming the count. They are never auto-submitted. |
| Two reviewers approve the same request | `decide()` is `FOR UPDATE` and returns `already_decided`; the second click shows it. |
| A bulk approve becomes a rubber stamp | The reason is mandatory and recorded on every row's audit event (U16), the sheet expands the exact ids, and rows filed by the current actor are unchecked by default. |
| Someone adds event retention and silently destroys the decision record | U18 makes `material_event` the only place a decision's reason lives. The existing rule that `prune_change_requests.py` never deletes events is now load-bearing, and the contract asserts it; any future event pruning has to keep the `approved` and `rejected` payloads. |
| A line break in `name` is rejected by the upstream | It is sent verbatim and flagged in the review sheet. A 4xx becomes a `failed` request with the upstream message, which is visible and retryable - not silent data loss. |
| The grid shows a value that is already stale | Every submit answer is authoritative; a `stale_base` triggers a row reload, not a silent overwrite. |
| Someone expects `approved` to mean `applied` | The queue labels the two states separately and the detail view says `apply is a worker run`, not a button. |
| A future mapping change adds a field the UI cannot edit | The editable surface comes from the server (U5), so a new field appears as read-only until an editor is added; it never appears as a wrong editor. |

## 15. Delivery plan

| Step | Work | Done when |
| --- | --- | --- |
| 1 | `submit()`: the `name`, `product_type` and `unit` trim includes newlines (`product_url` exempt), and any string value is capped at 1000 characters (U19, U20); `queue()` projects the decision reason from `material_event` (U18); contract assertions. No column, no migration | The contract asserts every refusal and the exemption, and the existing 37 write-back tests still pass |
| 2 | `GET /v1/writeback/schema` with `editor` hints, the five new `/v1/materials` parameters, and their tests plus `api_contract.sql` assertions | The schema endpoint matches the tables; every new filter is asserted in both SQL and Python |
| 3 | `POST /v1/change-requests/batch` with the reason rule, and `POST /v1/change-requests/decide-batch`, both with per-item results, the 100 cap and their tests | Partial success returns 200 with per-item codes; `reason_required` is enforced server-side; no new SQL beyond step 1 |
| 4 | `api/ui/` shell: layout, token and actor, query panel with facets and filter rows, saved views | A search and filter round trip works against a real database |
| 5 | The grid: paging, inline editors including the two textareas, staging, the review sheet with the reason rule, submit and its result states | Acceptance criteria 3 to 7 and 11 to 12 pass by hand on a seeded database |
| 6 | The Review tab: queue, detail with the three badges and the decision record, single and bulk approve and reject, actor switching | Acceptance criteria 8 to 10 and 13 to 14 pass |
| 7 | Docs: README section, this file's status, `docs/t3-progress.md` note, and a UI section in the design index if one exists | `check_english.py` green, links intact, PR opened |

## 16. Open questions

1. **Should a batch decision record a batch identity?** Today a bulk approve writes the same
   `decision_reason` on N rows and each keeps its own `decided_at`. Adding a `decision_batch` value
   would let an auditor say "these twenty were one action", at the cost of one more column and one
   more field in the response. It is deliberately not in v1.
2. **Does the reason rule belong in `submit()` rather than the HTTP edge?** The 5-material threshold
   is a policy, and policies live in SQL in this repository (D4). It sits in the route today because
   `submit()` handles one request at a time and cannot see the batch size. If a second client appears,
   the rule moves into a batch SQL function.
3. **Should `notes` be a first-class field?** It is an attribute today, allowed only for
   `board_materials` and `equipment_hire`. If it becomes the standard free-text slot for every
   category, it belongs in `material_writeback_field` and the mapping, not in nine `attribute_keys`
   lists.
4. **Does the 1000-character cap belong in `submit()` or in the mapping?** It is enforced in `submit()`
   today because that is the one gate every client passes. If joineryflow turns out to accept far
   less for `description`, the number moves into `mapping/joineryflow.yaml` next to the field, and the
   SQL reads it from there instead of holding a constant.
5. **Should the grid show `material_event` history for a row?** Deliberately not in v1: it is a third
   read path and a second drawer. The decision record in 8.3 covers "who approved this and why",
   which was the actual question.
