"""REST API v1 over the material library.

Transport only: parameters are parsed here, payloads are produced by SQL functions
(schema/search.sql, schema/api.sql), paging cursors and error mapping are the edge's job.
See docs/api-v1.md for the contract this implements.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Iterator

import psycopg
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

from . import db

log = logging.getLogger("material_library.api")

SCHEMA_VERSION = "2"
CURSOR_PREFIX = "o:"
EXPORT_COLUMNS = [
    "schema_version",
    "material_uid",
    "rev",
    "status",
    "superseded_by",
    "category_code",
    "product_type",
    "name",
    "description",
    "model_no",
    "brand",
    "supplier",
    "family_id",
    "family_name",
    "variant_values",
    "unit",
    "currency",
    "unit_cost",
    "cost_basis",
    "lead_time_days",
    "image_url",
    "product_url",
    "attributes",
    "tags",
    "synonyms",
    "measures",
    "identifiers",
    "prices",
    "relations",
    "classifications",
    "epd",
    "source_system",
    "source_id",
    "confidence",
    "created_at",
    "updated_at",
]


class ApiError(Exception):
    """A problem the caller caused or a dependency failed; rendered in the single error shape."""

    def __init__(self, status: int, code: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------------------
# Configuration and lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    config = db.settings()
    db.open_pool(config)
    app.state.config = config
    log.info("api started: port=%s pool=%s-%s", config["port"], db.POOL_MIN, db.POOL_MAX)
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(
    title="Material Library API",
    version="1.0",
    description="Read-only material catalogue: forgiving search, UMR v2 documents, sync feed.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Errors: one shape, always (docs/api-v1.md 3.3)
# ---------------------------------------------------------------------------
def _problem(status: int, code: str, message: str, detail: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if detail:
        body["error"]["detail"] = detail
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return _problem(exc.status, exc.code, exc.message, exc.detail)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {400: "bad_request", 401: "unauthorized", 404: "not_found", 405: "method_not_allowed"}.get(
        exc.status_code, "error"
    )
    return _problem(exc.status_code, code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return _problem(400, "bad_request", "invalid request parameters", {"errors": exc.errors()[:5]})


@app.exception_handler(psycopg.errors.InvalidParameterValue)
async def invalid_parameter_handler(_: Request, exc: psycopg.errors.InvalidParameterValue) -> JSONResponse:
    # ml_measure_predicate raises SQLSTATE 22023 with a message that names the measure.
    # message_primary drops the PL/pgSQL CONTEXT block, which would leak internals to the caller.
    message = exc.diag.message_primary or str(exc).strip()
    return _problem(400, "bad_request", message, {"sqlstate": exc.sqlstate})


@app.exception_handler(psycopg.errors.QueryCanceled)
async def timeout_handler(_: Request, exc: psycopg.errors.QueryCanceled) -> JSONResponse:
    return _problem(503, "unavailable", "database statement timed out", {"sqlstate": exc.sqlstate})


@app.exception_handler(psycopg.OperationalError)
async def unavailable_handler(_: Request, exc: psycopg.OperationalError) -> JSONResponse:
    log.warning("database unavailable: %s", exc)
    return _problem(503, "unavailable", "database is unavailable")


@app.exception_handler(Exception)
async def internal_handler(_: Request, exc: Exception) -> JSONResponse:
    # Stack trace stays in the server log; the caller never sees SQL or a Python traceback.
    log.exception("unhandled error")
    return _problem(500, "internal", "internal error")


# ---------------------------------------------------------------------------
# Request parsing helpers
# ---------------------------------------------------------------------------
def require_token(
    authorization: str | None = Header(default=None),
) -> None:
    """Bearer check, active only when ML_API_TOKEN is set (auth is out of scope for v1)."""
    config = app.state.config
    expected = config["token"]
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(401, "unauthorized", "Authorization: Bearer <token> is required")
    if authorization.split(" ", 1)[1].strip() != expected:
        raise ApiError(401, "unauthorized", "invalid token")


def actor_from(x_actor: str | None) -> str:
    config = app.state.config
    value = (x_actor or "").strip()
    return value or config["actor_default"]


def is_truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def log_search(
    conn: psycopg.Connection,
    *,
    q: str | None,
    level: str | None,
    total: int,
    filters: dict[str, Any],
    took_ms: int,
    actor: str,
    disabled: bool,
) -> int | None:
    """Record one search; returns search_log.id so the client can report a click.

    This is the only write on the read path and it must never fail the request: a logging
    problem is a metric loss, not a broken search (docs/api-v1.md 5).
    """
    config = app.state.config
    if disabled or not config["log_search"]:
        return None
    try:
        row = conn.execute(
            """
            INSERT INTO search_log (q, level, total, filters, took_ms, actor)
            VALUES (%(q)s, %(level)s, %(total)s, %(filters)s::jsonb, %(took_ms)s, %(actor)s)
            RETURNING id
            """,
            {
                "q": q,
                "level": level,
                "total": total,
                "filters": json.dumps(filters),
                "took_ms": took_ms,
                "actor": actor,
            },
        ).fetchone()
    except psycopg.Error as exc:  # noqa: BLE001 - logging is best effort
        log.warning("search logging failed: %s", exc)
        return None
    return int(row["id"]) if row else None


def parse_json_object(raw: str | None, name: str) -> dict[str, Any]:
    """attrs/measures are JSON objects; anything else is a 400 before it reaches the database."""
    if raw is None or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(400, "bad_request", f"{name} is not valid JSON", {"reason": exc.msg}) from exc
    if not isinstance(value, dict):
        raise ApiError(400, "bad_request", f"{name} must be a JSON object", {"got": type(value).__name__})
    return value


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"{CURSOR_PREFIX}{offset}".encode()).decode().rstrip("=")


def decode_cursor(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return 0
    token = raw.strip()
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ApiError(400, "bad_request", "cursor is not valid", {"cursor": token}) from exc
    if not decoded.startswith(CURSOR_PREFIX):
        raise ApiError(400, "bad_request", "cursor is not valid", {"cursor": token})
    try:
        offset = int(decoded[len(CURSOR_PREFIX):])
    except ValueError as exc:
        raise ApiError(400, "bad_request", "cursor is not valid", {"cursor": token}) from exc
    if offset < 0:
        raise ApiError(400, "bad_request", "cursor is not valid", {"cursor": token})
    return offset


def _search_payload(
    conn: psycopg.Connection,
    *,
    q: str | None,
    category: str | None,
    brand: str | None,
    supplier: str | None,
    attrs: dict[str, Any],
    measures: dict[str, Any],
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """One call to material_search_page(): the payload shape lives in SQL, not here."""
    row = db.query_one(
        conn,
        """
        SELECT material_search_page(
                   p_q => %(q)s,
                   p_category => %(category)s,
                   p_brand => %(brand)s,
                   p_supplier => %(supplier)s,
                   p_attrs => %(attrs)s::jsonb,
                   p_limit => %(limit)s,
                   p_offset => %(offset)s,
                   p_measures => %(measures)s::jsonb) AS payload
        """,
        {
            "q": q,
            "category": category,
            "brand": brand,
            "supplier": supplier,
            "attrs": json.dumps(attrs, sort_keys=True),
            "measures": json.dumps(measures, sort_keys=True),
            "limit": limit,
            "offset": offset,
        },
    )
    return row["payload"]


# ---------------------------------------------------------------------------
# Health and metadata
# ---------------------------------------------------------------------------
@app.get("/healthz", tags=["ops"])
def healthz() -> dict[str, str]:
    """200 or 503, never 500: a load balancer only needs to know whether the database answers."""
    try:
        with db.get_pool().connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - health must not raise
        log.warning("health check failed: %s", exc)
        return JSONResponse(status_code=503, content={"error": {"code": "unavailable", "message": "database is unavailable"}})  # type: ignore[return-value]
    return {"status": "ok"}


@app.get("/v1/meta", tags=["ops"], dependencies=[Depends(require_token)])
def meta(conn: psycopg.Connection = Depends(db.connection)) -> dict[str, Any]:
    """Lets a client detect a re-seeded or re-imported database."""
    row = db.query_one(
        conn,
        """
        SELECT count(*) AS materials,
               coalesce(max(sync_seq), 0) AS max_sync_seq,
               coalesce(max(updated_at), now()) AS updated_at
          FROM material
        """,
    )
    assert row is not None
    return {
        "schema_version": SCHEMA_VERSION,
        "materials": row["materials"],
        "max_sync_seq": row["max_sync_seq"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# Search and browse
# ---------------------------------------------------------------------------
@app.get("/v1/materials", tags=["materials"], dependencies=[Depends(require_token)])
def search_materials(
    q: str | None = Query(default=None, max_length=200, description="free text; omit to browse"),
    category: str | None = Query(default=None, description="category code, exact"),
    brand: str | None = Query(default=None),
    supplier: str | None = Query(default=None),
    attrs: str | None = Query(default=None, description='JSON object, e.g. {"colour":"Black"}'),
    measures: str | None = Query(
        default=None,
        description='JSON object, e.g. {"thickness_mm":{"gte":17.5,"lte":18.5,"unit":"mm"}}',
    ),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, description="opaque offset cursor from next_cursor"),
    expand: str = Query(default="variants", pattern="^variants$"),
    x_actor: str | None = Header(default=None, description="system name, never a person"),
    x_no_log: str | None = Header(default=None, description="1 disables search logging"),
    conn: psycopg.Connection = Depends(db.connection),
) -> dict[str, Any]:
    attrs_obj = parse_json_object(attrs, "attrs")
    measures_obj = parse_json_object(measures, "measures")
    offset = decode_cursor(cursor)
    query_text = (q or "").strip() or None

    started = time.perf_counter()
    payload = _search_payload(
        conn,
        q=query_text,
        category=category,
        brand=brand,
        supplier=supplier,
        attrs=attrs_obj,
        measures=measures_obj,
        limit=limit,
        offset=offset,
    )
    took_ms = int((time.perf_counter() - started) * 1000)

    total = int(payload.get("total") or 0)
    next_offset = offset + limit
    payload.update(
        {
            "schema_version": SCHEMA_VERSION,
            "took_ms": took_ms,
            "next_cursor": encode_cursor(next_offset) if next_offset < total else None,
        }
    )
    payload["log_id"] = log_search(
        conn,
        q=query_text,
        level=payload.get("level"),
        total=total,
        filters={
            key: value
            for key, value in {
                "category": category,
                "brand": brand,
                "supplier": supplier,
                "attrs": attrs_obj,
                "measures": measures_obj,
            }.items()
            if value
        },
        took_ms=took_ms,
        actor=actor_from(x_actor),
        disabled=is_truthy(x_no_log),
    )
    return payload


class ClickRequest(BaseModel):
    """A click is a client assertion: which result of which search was chosen, at what rank."""

    material_uid: str = Field(min_length=4, max_length=64)
    rank: int = Field(ge=1, le=1000)


@app.post(
    "/v1/searches/{log_id}/clicks",
    tags=["search"],
    status_code=202,
    dependencies=[Depends(require_token)],
)
def report_click(
    log_id: int,
    body: ClickRequest,
    conn: psycopg.Connection = Depends(db.connection),
) -> dict[str, Any]:
    """Attach a click to a logged search; the FK rejects ids that were never logged (doc 5)."""
    try:
        conn.execute(
            """
            INSERT INTO search_click (log_id, material_uid, rank)
            VALUES (%(log_id)s, %(uid)s, %(rank)s)
            ON CONFLICT (log_id, material_uid) DO NOTHING
            """,
            {"log_id": log_id, "uid": body.material_uid, "rank": body.rank},
        )
    except psycopg.errors.ForeignKeyViolation as exc:
        raise ApiError(404, "not_found", "search log entry not found", {"log_id": log_id}) from exc
    return {"ok": True, "log_id": log_id, "material_uid": body.material_uid, "rank": body.rank}


@app.get("/v1/materials/{uid}", tags=["materials"], dependencies=[Depends(require_token)])
def material_detail(
    uid: str,
    measures: str = Query(default="preferred", pattern="^(preferred|all)$"),
    conn: psycopg.Connection = Depends(db.connection),
) -> dict[str, Any]:
    row = db.query_one(
        conn,
        "SELECT material_detail(p_uid => %(uid)s, p_measures_all => %(all)s) AS doc",
        {"uid": uid, "all": measures == "all"},
    )
    if row is None or row["doc"] is None:
        raise ApiError(404, "not_found", "material_uid not found", {"uid": uid})
    return row["doc"]


@app.get("/v1/materials/{uid}/family", tags=["materials"], dependencies=[Depends(require_token)])
def material_family(uid: str, conn: psycopg.Connection = Depends(db.connection)) -> dict[str, Any]:
    row = db.query_one(conn, "SELECT material_family(p_uid => %(uid)s) AS doc", {"uid": uid})
    if row is None or row["doc"] is None:
        raise ApiError(404, "not_found", "no family for this material_uid", {"uid": uid})
    return row["doc"]


@app.get("/v1/facets", tags=["search"], dependencies=[Depends(require_token)])
def facets(
    q: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None),
    brand: str | None = Query(default=None),
    supplier: str | None = Query(default=None),
    attrs: str | None = Query(default=None),
    measures: str | None = Query(default=None),
    per_facet: int = Query(default=12, ge=1, le=100),
    conn: psycopg.Connection = Depends(db.connection),
) -> dict[str, Any]:
    rows = db.query_rows(
        conn,
        """
        SELECT facet, value, cnt
          FROM material_facets(
                   p_q => %(q)s,
                   p_category => %(category)s,
                   p_brand => %(brand)s,
                   p_supplier => %(supplier)s,
                   p_attrs => %(attrs)s::jsonb,
                   p_per_facet => %(per_facet)s,
                   p_measures => %(measures)s::jsonb)
        """,
        {
            "q": (q or "").strip() or None,
            "category": category,
            "brand": brand,
            "supplier": supplier,
            "attrs": json.dumps(parse_json_object(attrs, "attrs"), sort_keys=True),
            "measures": json.dumps(parse_json_object(measures, "measures"), sort_keys=True),
            "per_facet": per_facet,
        },
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "q": (q or "").strip() or None,
        "facets": [{"facet": row["facet"], "value": row["value"], "count": row["cnt"]} for row in rows],
    }


@app.get("/v1/suggest", tags=["search"], dependencies=[Depends(require_token)])
def suggest(
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=5, ge=1, le=50),
    conn: psycopg.Connection = Depends(db.connection),
) -> dict[str, Any]:
    rows = db.query_rows(conn, "SELECT * FROM material_suggest(%(q)s, %(limit)s)", {"q": q, "limit": limit})
    return {
        "schema_version": SCHEMA_VERSION,
        "q": q,
        "suggestions": [
            {"value": row["suggestion"], "similarity": round(float(row["sim"] or 0), 4)} for row in rows
        ],
    }


# ---------------------------------------------------------------------------
# Sync feed
# ---------------------------------------------------------------------------
@app.get("/v1/changes", tags=["sync"], dependencies=[Depends(require_token)])
def changes(
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=5000),
    conn: psycopg.Connection = Depends(db.connection),
) -> dict[str, Any]:
    rows = db.query_rows(
        conn,
        "SELECT * FROM material_changes(p_since => %(since)s, p_limit => %(limit)s)",
        {"since": since, "limit": limit},
    )
    total = int(rows[0]["total_available"]) if rows else 0
    next_since = int(rows[0]["next_since"]) if rows else since
    records = [
        {key: value for key, value in row.items() if key not in {"total_available", "next_since"}}
        for row in rows
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "since": since,
        "next_since": next_since,
        "total_available": total,
        "changes": records,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _export_csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return value


@app.get("/v1/export", tags=["sync"], dependencies=[Depends(require_token)])
def export(
    format: str = Query(default="jsonl", pattern="^(jsonl|csv)$"),
    include_archived: bool = Query(default=False),
    conn: psycopg.Connection = Depends(db.connection),
) -> StreamingResponse:
    """Stream the whole catalogue as JSONL (one UMR v2 document per line) or a fixed CSV.

    The response is not wrapped in an envelope: every JSONL line validates against
    schema/material.schema.json on its own, and the count travels in a header so the body can
    be streamed without a second pass.
    """
    count = int(
        db.query_value(
            conn,
            "SELECT count(*) FROM material WHERE %(all)s OR status <> 'archived'",
            {"all": include_archived},
        )
        or 0
    )
    exported_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    headers = {
        "X-Schema-Version": SCHEMA_VERSION,
        "X-Exported-At": exported_at,
        "X-Record-Count": str(count),
        "Content-Disposition": f'attachment; filename="materials-{exported_at[:10]}.{format}"',
    }
    media_type = "application/x-ndjson" if format == "jsonl" else "text/csv; charset=utf-8"

    def stream_jsonl() -> Iterator[str]:
        with db.get_pool().connection() as stream_conn:
            for row in stream_conn.execute(
                "SELECT doc FROM material_export(p_include_archived => %(all)s)",
                {"all": include_archived},
            ):
                yield json.dumps(row["doc"], ensure_ascii=False, sort_keys=True) + "\n"

    def stream_csv() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        header = False
        with db.get_pool().connection() as stream_conn:
            for row in stream_conn.execute(
                "SELECT doc FROM material_export(p_include_archived => %(all)s)",
                {"all": include_archived},
            ):
                doc = row["doc"]
                if not header:
                    writer.writerow(EXPORT_COLUMNS)
                    header = True
                writer.writerow([_export_csv_value(doc.get(column)) for column in EXPORT_COLUMNS])
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        if not header:
            writer.writerow(EXPORT_COLUMNS)
            yield buffer.getvalue()

    return StreamingResponse(
        stream_jsonl() if format == "jsonl" else stream_csv(), media_type=media_type, headers=headers
    )
