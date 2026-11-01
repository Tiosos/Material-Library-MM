# MCP server on the read API

Status: design, not implemented. This document is the plan for T3 (the MCP layer); the numbered
delivery steps at the end are the implementation checklist.

What it is: a small Model Context Protocol server that exposes the read API to LLM hosts
(Claude Desktop, Cursor, the Anthropic API with remote MCP, any MCP client). It adds no database
access and no SQL: every tool is one HTTP call to the FastAPI surface built in T1 and T1b, which is
the only place where search, collapse, validation and logging live. Read-only in v1.

Decisions M1-M4 were confirmed by the project owner before this document was written (section 14).

---

## 1. Why now

The read API is frozen and tested: 10 endpoints, a truthful `total`, family collapse, a versioned
export envelope, search logging, 37 tests (API plus merge), contract assertions and a search
evaluation gate.
The remaining gap is not capability, it is reachability. An LLM asked "how much is an 18 mm black
melamine board" cannot call `psql`, and giving it a database socket is the wrong shape: the API
already answers that question with a level, a total and a ranked hit list.

Three concrete reasons this belongs before the real-data ETL:

1. It is the cheapest integration in the roadmap (`docs/review-v1-and-plan-v2.md:257`: "one day of
   cost, and the benefit is AI can query materials directly").
2. It exercises the API as an external client would: if a tool description cannot explain
   `collapsed` or `measures`, the contract is still too complicated for humans too.
3. It creates a second, non-browser consumer of `search_log`. Queries an AI makes are exactly the
   long-tail phrasings the evaluation set is missing.

What it is **not**: a write surface. Merge, ignore, write-back and change requests stay behind
HTTP/CLI decisions a human makes (`docs/t2-merge.md` section 3: the keeper is always explicit). An
LLM proposing a merge is useful; an LLM performing one without review is not.

## 2. What exists today

| Piece | Where | Relevance |
| --- | --- | --- |
| Read API, 10 endpoints | `api/main.py` | The MCP server is a client of these routes; nothing else |
| Payloads in SQL | `schema/search.sql`, `schema/api.sql`, `schema/merge.sql` | Untouched: no new function, no new view |
| Auth | `ML_API_TOKEN` -> `Authorization: Bearer` (`api/main.py` `require_token`) | The MCP process is configured with the same token |
| Attribution | `X-Actor` header -> `search_log.actor` (`docs/api-v1.md` 5) | MCP traffic is distinguishable from web traffic |
| Search logging opt-out | `X-No-Log: 1` | Not used by search tools; kept for debugging |
| Error shape | `{"error": {"code", "message", "detail"?}}` | Mapped 1:1 into tool errors, no new vocabulary |
| Schema version | every payload carries `schema_version: "2"` | The MCP server refuses to guess; it passes it through |
| Python SDK | `mcp` 2.3.0 (pure-python wheel, `from mcp.server import MCPServer`) | Server, tools, resources, stdio and streamable HTTP transports |

The REST surface the tools wrap:

| Route | Purpose |
| --- | --- |
| `GET /healthz` | Liveness, no token |
| `GET /v1/meta` | Material count, `max_sync_seq`, `updated_at` |
| `GET /v1/materials` | Search and browse; `q`, `category`, `brand`, `supplier`, `attrs`, `measures`, `limit`, `cursor`, `expand` |
| `GET /v1/materials/{uid}` | Full UMR v2 document; `measures=preferred|all` |
| `GET /v1/materials/{uid}/family` | Family row plus its member records |
| `GET /v1/facets` | Facet counts for the same filters |
| `GET /v1/suggest` | Trigram suggestions |
| `GET /v1/changes` | Incremental feed by `sync_seq` |
| `GET /v1/export` | JSONL / CSV / JSON envelope (not exposed as a tool, see 4.4) |
| `POST /v1/searches/{log_id}/clicks` | Click feedback (not exposed in v1) |

## 3. Principles

1. **One transport hop, zero business logic.** A tool builds query parameters, calls one route,
   trims what a model does not need and returns. If a rule (collapse, unit conversion, paging cap)
   is not already in SQL or in the route, it does not belong in the MCP layer.
2. **Parity over convenience.** Field names, error codes and `schema_version` are the API's. A model
   that learns the tools should be able to read `docs/api-v1.md` and recognise everything.
3. **Read-only.** No tool mutates state. `POST` routes stay unreachable from MCP in v1.
4. **Attributable.** Every call carries `X-Actor: mcp` (or `MCP_ACTOR`), so `search_log` shows who
   asked. No IP, no user agent, no prompt text - same privacy rule as the API (`docs/api-v1.md` 5).
5. **Fail loudly, fail small.** An API error becomes a tool error carrying `code` and `message`
   verbatim; a tool never invents an empty result to look successful. A wrong filter must be
   visible to the model, otherwise it retries the same thing.
6. **No cache, no credentials.** The process holds a token and a base URL. It never opens a
   database connection, so `ML_DSN` is not part of its environment.

## 4. Tool surface

Seven tools, one per route that an LLM plausibly needs. Names are short, verb-first and stable:
renaming a tool is a breaking change for saved prompts, so they are part of the contract.

| Tool | Route | Inputs (all optional unless stated) | Returns |
| --- | --- | --- | --- |
| `search_materials` | `GET /v1/materials` | `q`, `category`, `brand`, `supplier`, `attrs` (object), `measures` (object), `limit` 1..50 (default 10), `cursor`, `expand` | `total`, `level`, `collapsed`, `suggestion`, `next_cursor`, `results[]` (trimmed hits) |
| `get_material` | `GET /v1/materials/{uid}` | `uid` (required), `measures` (`preferred`/`all`) | full UMR v2 document |
| `get_family` | `GET /v1/materials/{uid}/family` | `uid` (required) | family row and members |
| `list_facets` | `GET /v1/facets` | same filters as `search_materials`, `per_facet` 1..100 | `facets[]` of `{facet, value, count}` |
| `suggest_materials` | `GET /v1/suggest` | `q` (required), `limit` 1..50 | `suggestions[]` of `{value, similarity}` |
| `list_changes` | `GET /v1/changes` | `since` >= 0, `limit` 1..200 | `next_since`, `total_available`, `changes[]` |
| `catalogue_info` | `GET /v1/meta` | none | `materials`, `max_sync_seq`, `updated_at`, `schema_version` |

### 4.1 `search_materials` result shape

The route returns hit objects with eleven fields; a model needs four of them to decide what to open
next, and the rest on request. The tool therefore returns the hit list **trimmed to** `material_uid`,
`name`, `unit`, `unit_cost`, plus `variant_count` and `variants` (a family hit stands for several
records), and keeps the envelope fields (`total`, `level`, `collapsed`, `suggestion`, `next_cursor`,
`took_ms`, `log_id`) intact. `score`, `category`, `brand` and `supplier` are dropped: they explain
ranking, not the product, and a model that wants them calls `get_material`.

Trimming is the only transformation the MCP layer performs on a payload. It is documented in the
tool description, in the docstring and here, because a client that expects thirteen fields and gets
six has a right to be surprised.

### 4.2 Tool descriptions are part of the API

Each description answers three questions in at most ~30 words: what it returns, when to use it, and
what a caller must know to avoid a useless call. The rules that must appear somewhere:

- Search first, then `get_material` with the uid: the hit list is for choosing, the document is for
  answering.
- `q` is free text and the engine degrades gracefully (`exact`, `prefix`, `and`, `relaxed`,
  `fuzzy`, `browse`, `none`); a `level` of `fuzzy` or `relaxed` means "approximate", and `none`
  means "nothing matched", which is an answer, not an error.
- `measures` takes ranges with units: `{"thickness_mm": {"gte": 17.5, "lte": 18.5, "unit": "mm"}}`.
  The unit is required and converted server-side (mm, cm, m, g, kg, and so on).
- `attrs` is exact JSONB containment, so `{"colour": "Black"}` matches only that value.
- `collapsed` paging counts families: `total` is a group count unless `expand` is `variants`.

### 4.3 `attrs` and `measures` as objects, not strings

The REST route takes both as JSON-encoded query strings (because query strings are strings). A tool
input schema takes them as objects and encodes them once, in one place. The same parser as the API
is not reused (it lives in `api/main.py`); the MCP client sends `json.dumps(value, sort_keys=True)`
and lets the API answer `400 bad_request` for anything malformed, which keeps one validator.

### 4.4 Deliberately not exposed

| Route or idea | Why not |
| --- | --- |
| `GET /v1/export` | A full catalogue in tool output is context suicide; JSONL/CSV export is a batch job, not a chat action |
| `POST /v1/searches/{log_id}/clicks` | Click feedback needs a UI decision about what "clicked" means; deferring keeps v1 read-only |
| Duplicate queue / merge / ignore (`/v1/duplicates*`) | Writes. Revisit only with an audit story and explicit human confirmation in the host UI |
| Write-back / change requests (T3) | Not designed yet |
| A `sql_query` tool | Defeats the point of the API: validation, logging and the schema contract all vanish |

## 5. Resources

One templated resource, no listing:

| Resource | Returns |
| --- | --- |
| `materials://{uid}` | The full UMR v2 document, byte-identical to `GET /v1/materials/{uid}` |

Why a resource at all: hosts let a user attach a resource to a conversation, and a UMR document is
exactly the artefact a person would attach ("here is the board I mean"). A resource is also
referenceable by the model without spending a tool round trip. `resources/list` returns an empty
list on purpose: a catalogue is not a document, and returning 5,000 URIs would be a worse interface
than one search tool.

`materials://{uid}/family` is **not** exposed as a resource: `get_family` already returns it, and two
ways to reach the same document doubles the surface for no gain.

## 6. Transports and configuration

| Env | Default | Meaning |
| --- | --- | --- |
| `ML_API_BASE_URL` | `http://127.0.0.1:8848` | The API the tools call; scheme, host and port, no trailing slash |
| `ML_API_TOKEN` | empty | Sent as `Authorization: Bearer` when set; the API's own rule (`401` otherwise) |
| `MCP_ACTOR` | `mcp` | Becomes `X-Actor`; use `mcp:<host>` to separate clients |
| `MCP_TIMEOUT` | `10` | Seconds per HTTP call; a slower call is a tool error, not a hanging conversation |
| `MCP_TRANSPORT` | `stdio` | `stdio` for local hosts, `http` for a shared server |
| `MCP_HTTP_HOST` | `127.0.0.1` | Bind address for the HTTP transport |
| `MCP_HTTP_PORT` | `8849` | Bind port for the HTTP transport (8848 is the API's) |

Command line: `python -m mcp_server [--transport stdio|http] [--base-url URL]`, flags overriding the
environment. stdio is the default because it is the only transport a desktop host can start without
configuration; the HTTP mode is the same process with `MCP_TRANSPORT=http`, which is why the extra
dependency (uvicorn, already pinned by the API) is not a second implementation.

Host configuration (documented in `README.md`, not hard-coded anywhere):

```json
{
  "mcpServers": {
    "material-library": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {"ML_API_BASE_URL": "http://127.0.0.1:8848", "ML_API_TOKEN": "..."}
    }
  }
}
```

## 7. Architecture

```
mcp host --stdio/http--> mcp_server/server.py --httpx2--> api/main.py --psycopg--> PostgreSQL
                              |                              |
                          tools.py                      material_search_page(),
                          client.py                      material_detail(), ...
```

| File | Responsibility |
| --- | --- |
| `mcp_server/__init__.py` | Package marker, version string |
| `mcp_server/config.py` | Read and validate the environment table above; one `Settings` dataclass |
| `mcp_server/client.py` | `ApiClient`: base URL join, bearer header, `X-Actor`, timeout, JSON decoding, `ApiError` -> tool error, no retries in v1 |
| `mcp_server/tools.py` | The seven tool functions; each builds a query dict, calls `ApiClient`, trims per 4.1 |
| `mcp_server/server.py` | `MCPServer("material-library")`, `@mcp.tool()` registrations, the `materials://{uid}` resource |
| `mcp_server/__main__.py` | argparse, transport selection, `mcp.run(transport=...)` |

Rules that keep it reviewable:

- No `import psycopg`, no `import api`, no SQL literal anywhere in `mcp_server/`. A grep in CI
  (`grep -r "psycopg" mcp_server/` must be empty) proves the separation.
- `ApiClient` is the only place that knows the API's URL and token. Tools receive a client, so a
  test can hand them one backed by `httpx2.ASGITransport` (no socket, no port).
- Tool functions return plain dicts; serialisation is the SDK's job, so the shapes are testable
  without an MCP client.

## 8. Error model

An API error becomes a tool error with the API's `code` and `message` untouched, plus `detail` when
present:

| API | Tool result |
| --- | --- |
| `404 not_found` (unknown uid) | isError, `code: not_found`, message names the uid |
| `400 bad_request` (bad `attrs`, bad unit, bad cursor) | isError, `code: bad_request`, the API sentence verbatim |
| `401 unauthorized` | isError, `code: unauthorized`, "check ML_API_TOKEN" |
| `503 unavailable` (database down, statement timeout) | isError, `code: unavailable`, "the API is not reachable; retry later" |
| connection refused / timeout | isError, `code: unavailable`, the exception's class and the base URL, no stack trace |

Two rules: a tool never converts an error into an empty result, and it never returns a traceback.
The model sees the same vocabulary as a human reading `docs/api-v1.md` 3.3, which is the whole point
of the wrapper.

## 9. Instrumentation and privacy

`search_materials` is a real search: `search_log` gets a row with `q`, `level`, `total`, `filters`,
`took_ms` and `actor = MCP_ACTOR`, and the payload's `log_id` is returned so a host that shows
results can send clicks later (clicks are not exposed in v1, but the id costs nothing). The other
six tools never create log rows: `get_material` and `get_family` are lookups by uid, `list_facets`
and `suggest_materials` are not logged by the API either (`docs/api-v1.md` 5).

No prompt text, no user identity, no client name beyond `MCP_ACTOR`. The MCP server logs one line
per tool call at INFO (tool, duration, status) to stderr, which is where stdio hosts expect logs -
stdout carries the protocol and must stay clean.

## 10. Evaluation

`tests/test_mcp.py`, pytest, no network:

| Assertion | How |
| --- | --- |
| The server registers exactly the seven tools, names and descriptions within limits | list tools through an in-memory client session (exact SDK entry point confirmed in step 0) |
| Every tool answers against a prepared database | `httpx2.ASGITransport(app=api.main.app)` as the client transport |
| `search_materials` returns the trimmed hit shape and the envelope fields | `q=laminex` -> `total >= 1`, hits have `name`/`unit`, no `score` |
| `measures` is accepted as an object and a bad unit is a tool error, not an empty page | `{"thickness_mm": {"gte": 17.5, "lte": 18.5, "unit": "mm"}}`; `unit: "kg"` -> isError `bad_request` |
| `get_material` validates against `schema/material.schema.json` | `jsonschema` on the returned document |
| Unknown uid is an error carrying `not_found` | `get_material("mat_doesnotexist")` |
| The resource template returns the same document as the tool | `materials://mat_...` vs `get_material` |
| A token-protected API is honoured | monkeypatch `ML_API_TOKEN`, expect `unauthorized` without it |
| English only, ASCII | `scripts/check_english.py` covers the new files |

`tests/test_api.py` stays the API's own suite; the MCP suite adds no HTTP server and no port, so it
runs in the same CI job after the contract assertions.

## 11. Delivery

| Step | Work | Done when |
| --- | --- | --- |
| 0 | `mcp_server/` skeleton: config, `ApiClient`, `__main__` with `--help`; `mcp==2.3.0` in `mcp_server/requirements.txt`. Confirm the SDK 2.x entry points against the installed wheel (`MCPServer`, how a server is run per transport, and the in-memory client used by the tests) and record them in the progress log | `python -m mcp_server --help` prints the flags; `grep -r psycopg mcp_server/` is empty; the three SDK entry points are named in the log with the file:line they were read from |
| 1 | Seven tools in `tools.py` + registrations in `server.py` | A local stdio session lists seven tools and `search_materials` answers against a running API |
| 2 | `materials://{uid}` resource, `--transport http` | The same document arrives as a resource and as a tool result; HTTP mode answers `initialize` |
| 3 | `tests/test_mcp.py` (in-memory transport) and a CI step in the `api` job | `pytest -q tests` includes the MCP cases and is green |
| 4 | README host snippet, `docs/data-model-v2.md` B8 row, progress log, `check_english.py` | Docs and CI agree; commit and PR |

Effort: 1.5 days, matching the estimate in `docs/review-v1-and-plan-v2.md:257`.

## 12. Risks and non-goals

| Risk | Impact | Mitigation |
| --- | --- | --- |
| MCP specification churn | Medium | Pin `mcp==2.3.0`; the SDK is the only protocol code, tools are plain functions |
| Tool descriptions drift from the API | Medium | Descriptions live next to the tools; `tests/test_mcp.py` asserts the tool list and that `measures`/`attrs` are documented |
| A model passes an unbounded `limit` | Low | The input schema caps `limit` at 50 (200 for `list_changes`); the API caps again |
| Context blow-up from a huge document | Medium | Hits are trimmed (4.1); documents are per uid; export is not exposed |
| A second consumer forgets `X-Actor` | Low | The header is set in one place (`ApiClient`), not per tool |
| HTTP mode without auth | Medium | Token comes from the environment; binding defaults to 127.0.0.1; the README says to use a reverse proxy for anything public |
| The MCP server becomes a place for business logic | High (maintenance) | The rule in section 3.1 and the `psycopg` grep in CI |

Non-goals for v1: no writes, no export tool, no streaming results, no caching, no prompt templates,
no multi-tenant auth, no semantic/vector search, no click reporting.

## 13. Acceptance criteria

1. `python -m mcp_server` over stdio lists seven tools and one resource template; a tool call
   without a running API returns `unavailable`, not a traceback.
2. `search_materials("laminex 18mm", measures={"thickness_mm": {"gte": 17.5, "lte": 18.5, "unit":
   "mm"}})` returns the same uid set as `GET /v1/materials` with the same filters.
3. Every hit carries `material_uid`, `name`, `unit`, `unit_cost`, `variant_count`, `variants`; the
   envelope carries `total`, `level`, `collapsed`, `suggestion`, `next_cursor`, `log_id`.
4. `get_material(uid)` returns a document that validates against `schema/material.schema.json`.
5. `materials://{uid}` returns the same document as `get_material(uid)`.
6. `X-Actor: mcp` appears in `search_log.actor` for a search made through the tools.
7. `python -m pytest -q tests` is green with the MCP cases, and `python scripts/check_english.py`
   passes for the new files.

## 14. Decisions

| # | Decision | Why |
| --- | --- | --- |
| M1 | stdio is the default transport, streamable HTTP is selectable with `MCP_TRANSPORT=http` | Local hosts need zero setup; a shared server is the same process with one flag, so the second transport costs uvicorn only (already installed) |
| M2 | Read-only tools in v1: seven GET routes, no merge/ignore/write-back | An LLM may propose a merge; a human decides it (`docs/t2-merge.md` 3). Writes need an audit story first |
| M3 | The server calls the REST API, not SQL | One validator, one logger, one error shape. The API is the contract; a second SQL path would drift and would need `ML_DSN` in the MCP process |
| M4 | One templated resource `materials://{uid}` and no resource listing | A UMR document is the artefact a person attaches; a catalogue is reached with a search tool |
| M5 | `search_materials` trims hits to six fields and documents the trim | Eleven fields per hit is context for no decision; the full document is one tool call away |
| M6 | Errors pass through with the API's `code` and `message`, never invented, never swallowed | The model must see "bad unit" to fix its argument instead of retrying |
| M7 | `mcp==2.3.0`, pure-python wheels, no `[cli]` extra | Reproducible and installable the same way as the API's stack; `mcp dev` is a developer convenience, not a runtime need |
| M8 | Actor defaults to `mcp`, configurable to `mcp:<host>` | `search_log` already separates systems (`joineryflow-web`); MCP traffic should be its own stream |
| M9 | No cache in v1 | The catalogue is small and the API is fast; a stale cache is a correctness bug that outlives the latency win |

## 15. Open questions

None blocking. The questions that would change the design if the answer arrived now:

- **Click feedback.** Should the host report clicks? That needs a UI decision (what counts as a
  click), so the route stays out of v1 even though `log_id` is returned.
- **Merge proposals.** A read-only `propose_merge` tool (queue + preview, no write) would be safe
  and useful; it is not in v1 to keep the surface at "what the read API does".
- **Streaming search.** MCP supports streamed results; a 50-hit page does not need them. Revisit if
  hosts start streaming long exports, which v1 does not expose.
