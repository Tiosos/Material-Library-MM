# MCP server - implementation log

The plan is `docs/mcp-server.md`. This file records what each step actually did, the commands that
proved it, and every place the plan met reality and changed.

A step is only done when its row below carries a verification line: a command, its output, and the
database or process it ran against.

## Steps

| Step | Work | State |
| --- | --- | --- |
| 0 | Skeleton: config, `ApiClient`, `__main__`, `requirements.txt`; confirm the SDK entry points | [x] |
| 1 | Seven tools in `tools.py`, registered in `server.py` | [ ] |
| 2 | `materials://{uid}` resource, `--transport http` | [ ] |
| 3 | `tests/test_mcp.py` plus a CI step in the `api` job | [ ] |
| 4 | README host snippet, data-model row, this log, `check_english.py` | [ ] |

## Verification environment

| Item | Value |
| --- | --- |
| PostgreSQL | 16.15 on `127.0.0.1:55432`, started as a background job: `.pg/bin/postgres.exe -D .pgdata -p 55432 -c listen_addresses=127.0.0.1 -c logging_collector=off` |
| Fresh database | `python .tmp/newdb.py ml_tN [both\|fixture\|blum]`; databases are never dropped (`DROP DATABASE` fails in this sandbox) |
| SQL files | `python .tmp/run_sql.py <file.sql> --db ml_tN` |
| Python packages | `PYTHONPATH=D:\material library\.pylibs`; the sandbox blocks pip, so `.tmp/resolve.py` vendors wheels |
| API under test | `ML_DSN=postgresql://postgres@127.0.0.1:55432/ml_tN`, `python -m uvicorn api.main:app --port 8851` |
| MCP server | `python -m mcp_server --transport stdio` (a host starts it; tests use the in-memory transport) |

## Step 0 - skeleton and SDK entry points

Shipped:

- `mcp_server/__init__.py` - package marker and `__version__ = "0.1.0"`.
- `mcp_server/config.py` - `Settings` dataclass with the six knobs of `docs/mcp-server.md` 6,
  `from_env()` and `apply_overrides()`; every value is validated at startup (`ML_API_BASE_URL`
  needs a scheme, `MCP_TRANSPORT` must be `stdio` or `http`, `MCP_TIMEOUT` is 0.1-300 s,
  `MCP_HTTP_PORT` is 1-65535, `MCP_ACTOR` is at most 64 characters) and a bad value is a
  `ConfigError` the entry point turns into exit code 2.
- `mcp_server/client.py` - `ApiClient` and `ApiError`. `clean_params()` drops unset values and
  encodes `attrs`/`measures` objects once as compact JSON; `get()` maps HTTP errors to `ApiError`
  carrying the API's own `code`, `message` and `detail`, and transport failures to
  `code="unavailable"`; `ApiError.to_tool_error()` renders `code: message (detail)`.
- `mcp_server/server.py` - `build_server(settings, api=None)` returns `MCPServer("material-library",
  version=..., instructions=...)` and stores the client on the instance. No tools yet: step 0
  exists to prove the transports, not the surface.
- `mcp_server/__main__.py` - `argparse` with `--transport {stdio,http}`, `--base-url`, `--version`;
  stdio by default, `streamable-http` on `MCP_HTTP_HOST:MCP_HTTP_PORT` otherwise.
- `mcp_server/requirements.txt` - `mcp==2.3.0`, `mcp-types==2.3.0`, `httpx2==2.16.0`,
  `sse-starlette==3.6.0`, `pywin32==312` on Windows.

### SDK entry points, read from the installed wheel

Recorded as the plan requires (`docs/mcp-server.md` 11, step 0). Paths are inside the vendored
`.pylibs` because that is where the wheel was unpacked; the line numbers are from `mcp` 2.3.0.

| Need | Entry point | Where |
| --- | --- | --- |
| The server object | `from mcp.server import MCPServer` | `.pylibs/mcp/server/mcpserver/server.py:157` |
| Register a tool | `@server.tool(name=None, title=None, description=None, annotations=None, icons=None, meta=None, structured_output=None)` | `.pylibs/mcp/server/mcpserver/server.py:669` |
| Register a resource or template | `@server.resource(uri, *, name=None, title=None, description=None, mime_type=None, ...)`; a URI containing `{param}` becomes a template whose handler receives that parameter | `.pylibs/mcp/server/mcpserver/server.py:788` |
| Run it | `server.run(transport="stdio")`, `server.run("streamable-http", host=..., port=...)`, or the `run_stdio_async()` / `run_streamable_http_async(host, port, streamable_http_path="/mcp")` coroutines | `.pylibs/mcp/server/mcpserver/server.py:404` |
| A synchronous tool body | allowed; the SDK calls it with `anyio.to_thread.run_sync` | `.pylibs/mcp/server/mcpserver/utilities/func_metadata.py:164` |
| An anticipated tool failure | `raise ToolError("...")` from `mcp.server.mcpserver.exceptions`; the call returns `is_error=True` with the text and logs at INFO without a traceback | `.pylibs/mcp/server/mcpserver/exceptions.py` |
| In-memory client for tests | `from mcp.client import Client`; `Client(server)` accepts an `MCPServer` instance and runs it in-process - `async with Client(server) as c: await c.list_tools()`, `await c.call_tool(name, args)`, `await c.read_resource(uri)` | `.pylibs/mcp/client/client.py:266` |
| stdio client | `Client(StdioServerParameters(command=..., args=[...], env=..., cwd=...))` | `.pylibs/mcp/client/stdio.py` |

Two naming details that cost time and are worth writing down: `list_resource_templates()` returns
`resource_templates` (snake case) while `list_tools()` returns `tools`, and `call_tool()` results
carry the payload as `content[0].text` holding JSON (there is no `structuredContent` for a tool
whose return annotation is a plain `dict`).

### Deviation - the dependency pin had to move (decision M10)

`mcp==2.3.0` requires `sse-starlette>=3.0.0`; `sse-starlette` 3.x requires `starlette>=0.49.1`. The
API was pinned to `fastapi==0.115.6` + `starlette==0.41.3`, and `fastapi 0.115.6` caps starlette
below 0.42. The two cannot be installed in one environment, and `docs/mcp-server.md` 10 requires the
MCP tests to run in the same job as the API tests.

Options weighed:

1. Upgrade the API stack to `fastapi 0.162.0` + `starlette 0.52.1` (chosen).
2. Keep the API pin and give the MCP server its own virtual environment and CI job.
3. Downgrade `mcp` to a 1.x release whose starlette floor is lower.

Option 1 was verified before being chosen: with the upgraded wheels the API suite passes unchanged
(`37 passed`), `mcp 2.3.0` imports, and the streamable-HTTP transport answers `initialize` with
HTTP 200. Option 2 doubles the environments and breaks the single-job design; option 3 contradicts
the entry points the plan already recorded. `fastapi 0.162.0` depends on `opentelemetry-api`
directly, so that is pinned too rather than worked around.

`api/requirements.txt` therefore now pins `fastapi==0.162.0`, `starlette==0.52.1` and
`opentelemetry-api==1.45.2`, and `mcp_server/requirements.txt` pins the MCP side.

### Verification

| Check | Command | Result |
| --- | --- | --- |
| Flags parse and exit cleanly | `python -m mcp_server --help` | usage with `--transport`, `--base-url`, `--version`; rc 0 |
| Version flag | `python -m mcp_server --version` | `mcp_server 0.1.0`; rc 0 |
| No database import in the package | `grep -rn "psycopg" mcp_server/` | no output (rc 1), so the CI rule holds |
| In-memory session works | `Client(build_server(Settings()))` | `tools: []`, `server_info: material-library 0.1.0`, `resources: []` |
| A real stdio process works | `Client(StdioServerParameters(command=sys.executable, args=["-m", "mcp_server"], cwd=...))` | `stdio tools: []`, `stdio server: material-library 0.1.0`, instructions delivered |
| HTTP transport answers | `server.run("streamable-http", port=8871)` plus a raw `POST /mcp` `initialize` | HTTP 200, `event: message` with `protocolVersion 2025-06-18` and `serverInfo.name` `probe` |
| API suite unaffected by the upgrade | `ML_DSN=.../ml_mcp1 python -m pytest -q tests -p no:cacheprovider` | `37 passed, 1 warning in 4.40s` |
| English only | `python scripts/check_english.py` | `OK: no CJK text found` |

The 1 warning is `starlette/testclient.py:45: DeprecationWarning: The anyio.abc.BlockingPortal alias
is deprecated` - it comes from starlette 0.52.1 itself, not from this repository.
