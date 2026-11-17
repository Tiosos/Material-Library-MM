#!/usr/bin/env python3
"""One command to run the real, interactive Material Library UI locally.

    python scripts/start_ui.py

What it does, in order:
  1. Check Docker is available and the PostgreSQL container (default
     `ml-postgres16`) is running; start it if it exists but is stopped.
  2. Create the preview database (default `ml_preview`) and load every schema
     in CI order, including create-material.sql. Skip this when the database
     already has materials, so a second run keeps any requests you filed.
  3. Import the two sample supplier catalogues (Blum and Laminex) if empty.
  4. Start uvicorn on 127.0.0.1:8765 and print the UI URL. Press Ctrl-C to stop.

The script is idempotent and never drops a database. It needs Docker, the
project venv (or the packages in api/requirements.txt) and nothing else. CI
uses the same schema order and imports, so a local preview matches the tests.

Options:
  --host, --port, --db, --container, --dsn, --skip-db, --no-browser
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA_ORDER = [
    "material.sql", "search.sql", "api.sql", "merge.sql",
    "seed.sql", "writeback.sql", "create-material.sql",
]
IMPORTS = [
    ("ingest/samples/blum_hinges.csv", "mappings/blum.yaml"),
    ("ingest/samples/laminex_boards.csv", "mappings/laminex.yaml"),
]
DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"


def _connect(dsn):
    import psycopg
    return psycopg.connect(dsn, autocommit=True)


def check_docker(container: str) -> None:
    if shutil.which("docker") is None:
        raise SystemExit("docker not found on PATH; install Docker Desktop and retry")
    state = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True, text=True,
    )
    if state.returncode != 0:
        raise SystemExit(
            f"container '{container}' not found. Create it with:\n"
            f"  docker run -d --name {container} -e POSTGRES_PASSWORD=postgres "
            f"-e POSTGRES_DB=ml -p 5432:5432 postgres:16"
        )
    if state.stdout.strip() != "true":
        print(f"[db] starting container '{container}' ...")
        subprocess.run(["docker", "start", container], check=True)
        time.sleep(2)


def database_exists(admin_dsn: str, db: str) -> bool:
    with _connect(admin_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db,))
            return cur.fetchone() is not None


def prepare_database(dsn: str, db: str) -> str:
    """Create the DB and load schemas/imports. Return the preview DB DSN."""
    preview_dsn = dsn.rsplit("/", 1)[0] + "/" + db
    if database_exists(dsn, db):
        with _connect(preview_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('material')")
                if cur.fetchone()[0] is not None:
                    cur.execute("SELECT count(*) FROM material")
                    count = cur.fetchone()[0]
                    if count:
                        print(f"[db] '{db}' already has {count} materials; reusing it")
                        return preview_dsn
        # DB exists but is empty: fall through and load it.
        print(f"[db] '{db}' exists but is empty; loading schemas")
    else:
        print(f"[db] creating '{db}'")
        with _connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{db}"')

    with _connect(preview_dsn) as conn:
        for name in SCHEMA_ORDER:
            path = ROOT / "schema" / name
            print(f"[db] load schema/{name}")
            conn.execute(path.read_text(encoding="utf-8"))

    for csv, mapping in IMPORTS:
        print(f"[db] import {Path(csv).name}")
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/import_file.py"),
             "--file", str(ROOT / csv), "--mapping", str(ROOT / mapping),
             "--dsn", preview_dsn, "--apply"],
            check=True, cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
        )
    return preview_dsn


def start_uvicorn(host: str, port: int, preview_dsn: str, no_browser: bool) -> int:
    env = {**os.environ, "ML_DSN": preview_dsn}
    url = f"http://{host}:{port}/ui"
    print(f"\n[ui] starting {url}  (Ctrl-C to stop)")
    if not no_browser:
        # Give uvicorn a moment before opening the browser.
        def open_later():
            time.sleep(2)
            webbrowser.open(url)
        import threading
        threading.Thread(target=open_later, daemon=True).start()

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app",
         "--host", host, "--port", str(port)],
        cwd=str(ROOT), env=env,
    )
    try:
        return proc.wait()
    except KeyboardInterrupt:
        print("\n[ui] stopping ...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default="ml_preview", help="preview database name")
    ap.add_argument("--container", default="ml-postgres16", help="Docker PostgreSQL container")
    ap.add_argument("--dsn", default=os.environ.get("ML_ADMIN_DSN", DEFAULT_DSN),
                    help="admin DSN (default: postgres DB on localhost:5432)")
    ap.add_argument("--skip-db", action="store_true", help="assume the database is ready")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    args = ap.parse_args(argv)

    try:
        import psycopg  # noqa: F401
    except ImportError:
        raise SystemExit("psycopg is not installed; run: pip install -r api/requirements.txt")

    admin_dsn = args.dsn
    preview_dsn = admin_dsn.rsplit("/", 1)[0] + "/" + args.db
    if args.skip_db:
        print(f"[db] --skip-db: using {args.db} as-is")
    else:
        check_docker(args.container)
        preview_dsn = prepare_database(admin_dsn, args.db)

    return start_uvicorn(args.host, args.port, preview_dsn, args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
