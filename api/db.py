"""Database access for the HTTP edge.

One pool, one connection per request, no material logic. Every payload the API returns is
produced by a SQL function in schema/search.sql or schema/api.sql, so that psql can assert the
contract without starting a server (docs/api-v1.md 2).
"""

from __future__ import annotations

import os
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# The API is a read-only surface with one short write (search_log/search_click), so the pool is
# small on purpose: a local library server has single-digit clients.
POOL_MIN = 1
POOL_MAX = 8
STATEMENT_TIMEOUT = "3s"
APPLICATION_NAME = "material-library-api"


class ConfigError(RuntimeError):
    """Raised when the process is misconfigured; the process should not start."""


def settings() -> dict[str, Any]:
    """Environment configuration, read once at startup (docs/api-v1.md 8)."""

    dsn = os.environ.get("ML_DSN", "").strip()
    if not dsn:
        raise ConfigError(
            "ML_DSN is required, e.g. postgresql://postgres@127.0.0.1:55432/material_library"
        )
    return {
        "dsn": dsn,
        "host": os.environ.get("ML_API_HOST", "127.0.0.1"),
        "port": int(os.environ.get("ML_API_PORT", "8848")),
        "token": os.environ.get("ML_API_TOKEN", "").strip(),
        "actor_default": os.environ.get("ML_ACTOR_DEFAULT", "anonymous").strip() or "anonymous",
        "log_search": os.environ.get("ML_LOG_SEARCH", "1").strip() not in {"0", "false", "off"},
    }


def _configure(conn: psycopg.Connection) -> None:
    """Per-connection session settings: a runaway query must not pin a request."""
    conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
    conn.execute(f"SET application_name = '{APPLICATION_NAME}'")


_pool: ConnectionPool | None = None


def open_pool(config: dict[str, Any]) -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            config["dsn"],
            min_size=POOL_MIN,
            max_size=POOL_MAX,
            kwargs={"autocommit": True, "row_factory": dict_row, "connect_timeout": 5},
            configure=_configure,
            open=False,
            name=APPLICATION_NAME,
        )
        _pool.open(wait=True, timeout=10)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise ConfigError("database pool is not open")
    return _pool


def connection() -> Iterator[psycopg.Connection]:
    """FastAPI dependency: one connection per request, returned to the pool afterwards."""
    with get_pool().connection() as conn:
        yield conn


def query_rows(conn: psycopg.Connection, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
    return conn.execute(sql, params).fetchall()


def query_one(conn: psycopg.Connection, sql: str, params: dict[str, Any] | None = None) -> dict | None:
    rows = conn.execute(sql, params).fetchall()
    return rows[0] if rows else None


def query_value(conn: psycopg.Connection, sql: str, params: dict[str, Any] | None = None) -> Any:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return next(iter(row.values()))
