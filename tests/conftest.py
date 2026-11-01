"""Shared fixtures for the HTTP smoke tests.

The tests talk to a real database: the contract they assert (paging totals, level names,
schema-valid documents) is produced by SQL, so a mocked connection would assert nothing.
CI prepares the database with schema/*.sql plus eval/fixture.sql (docs/api-v1.md 9).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from api.main import app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def dsn() -> str:
    value = os.environ.get("ML_DSN", "").strip()
    if not value:
        pytest.skip("ML_DSN is not set; see docs/api-v1.md section 9 for the CI recipe")
    return value


@pytest.fixture(scope="session")
def client(dsn: str) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def conn(dsn: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(dsn, autocommit=True) as connection:
        yield connection


@pytest.fixture(scope="session")
def material_schema() -> dict[str, Any]:
    return json.loads((ROOT / "schema" / "material.schema.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def export_columns() -> list[str]:
    from api.main import EXPORT_COLUMNS

    return EXPORT_COLUMNS
