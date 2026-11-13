#!/usr/bin/env python3
"""CLI: apply approved write-back requests to joineryflow (T3, step 3).

Design: docs/t3-writeback.md sections 3, 8 and 9.

    python scripts/apply_change_requests.py --dsn "$ML_WRITEBACK_DSN" --dry-run --json -
    python scripts/apply_change_requests.py --dsn "$ML_WRITEBACK_DSN" --apply
    python scripts/apply_change_requests.py --dsn "$ML_WRITEBACK_DSN" --retry 12
    python scripts/apply_change_requests.py --dsn "$ML_WRITEBACK_DSN" --verify 12
    python scripts/apply_change_requests.py --dsn "$ML_WRITEBACK_DSN" --verify 12 --resolve

Dry-run is the default and is a true no-op: zero writes to
material_change_request, zero material_event rows and zero HTTP calls. It still
reports the exact URL, headers and body that --apply would send. --apply needs
ML_JF_BASE_URL and ML_JF_TOKEN; a missing setting exits 2 before the database
is opened. --verify GETs one unverifiable request and reports whether the
upstream shows the approved values; it is read-only unless --resolve is given,
and a missing upstream column is reported as unreachable, never as a mismatch.
The executor sends one PATCH per claimed request and settles every
outcome through SQL; it never decides a state transition itself (D4).

CI never egresses to joineryflow: tests point ML_JF_BASE_URL at a stdlib
http.server on 127.0.0.1 with an ephemeral port and inject a dummy token. No
real upstream URL is hard-coded here, and the token never appears in a report.

Exit codes: 0 success, 1 any failed/unknown outcome or runtime error,
2 usage or configuration error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "mapping" / "joineryflow.yaml"

USAGE = 2
FAILURE = 1
DEFAULT_LEASE = "5 minutes"
DEFAULT_TIMEOUT = 5.0
DEFAULT_ACTOR = "executor"


class ConfigError(RuntimeError):
    """A missing or contradictory setting; the process must not start."""


class TransportError(RuntimeError):
    """A timeout or connection failure: the upstream outcome is unknown."""


@dataclass(frozen=True)
class Config:
    mode: str
    workspace: str
    base_url: str | None = None
    token: str | None = None
    timeout: float = DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# Configuration and the workspace guard
# ---------------------------------------------------------------------------

def resolve_workspace(mapping_path: Path | None = None) -> str:
    """The configured workspace must agree with the committed snapshot header.

    docs/t3-writeback.md 4.2: the UMR has no namespace column, so a mismatch
    between ML_JF_WORKSPACE and the snapshot's workspace_slug is a hard error
    at executor startup rather than a runtime surprise.
    """
    import yaml

    rules = yaml.safe_load((mapping_path or MAPPING).read_text(encoding="utf-8"))
    source = rules.get("source") or {}
    expected = str(source.get("workspace") or "").strip()
    snapshot = ROOT / source.get("snapshot", "fixtures/joineryflow-hartwood.jsonl")
    with snapshot.open(encoding="utf-8") as fh:
        header = json.loads(fh.readline())
    header_slug = str(header.get("workspace_slug") or "").strip()

    if not expected or not header_slug or expected != header_slug:
        raise ConfigError(
            f"mapping source.workspace ({expected!r}) disagrees with the snapshot "
            f"header workspace_slug ({header_slug!r})"
        )
    configured = os.environ.get("ML_JF_WORKSPACE", "").strip() or expected
    if configured != header_slug:
        raise ConfigError(
            f"ML_JF_WORKSPACE ({configured!r}) does not match the snapshot workspace "
            f"({header_slug!r}); refusing to write into a foreign workspace"
        )
    return configured


def build_config(args: argparse.Namespace) -> Config:
    mode = ("retry" if args.retry else
            "verify" if args.verify else
            "apply" if args.apply else "dry-run")
    workspace = resolve_workspace()
    if mode not in ("apply", "verify"):
        return Config(mode=mode, workspace=workspace)

    base_url = os.environ.get("ML_JF_BASE_URL", "").strip()
    token = os.environ.get("ML_JF_TOKEN", "").strip()
    if not base_url:
        raise ConfigError(f"--{mode} requires ML_JF_BASE_URL; no live endpoint is assumed")
    if not token:
        raise ConfigError(f"--{mode} requires ML_JF_TOKEN")
    raw_timeout = os.environ.get("ML_JF_TIMEOUT", "").strip()
    try:
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT
    except ValueError as exc:
        raise ConfigError(f"ML_JF_TIMEOUT must be a number (got {raw_timeout!r})") from exc
    if timeout <= 0 or timeout > 60:
        raise ConfigError("ML_JF_TIMEOUT must be between 0 and 60 seconds")
    return Config(mode=mode, workspace=workspace, base_url=base_url,
                  token=token, timeout=timeout)


# ---------------------------------------------------------------------------
# Transport: one PATCH, no retries, no credential in any report
# ---------------------------------------------------------------------------

class JoineryflowClient:
    """Thin urllib PATCH and GET. Transport errors surface as TransportError."""

    def __init__(self, base_url: str, token: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def patch(self, path: str, headers: dict, body: dict) -> tuple[int | None, str]:
        outgoing = {str(k): str(v) for k, v in headers.items()}
        outgoing["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body, sort_keys=True).encode("utf-8"),
            headers=outgoing,
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except OSError as exc:
            raise TransportError(str(exc)) from exc

    def get(self, path: str) -> tuple[int | None, str]:
        """Read the upstream row for --verify; the same auth, no body."""
        req = urllib.request.Request(
            self.base_url + path,
            headers={"Authorization": "Bearer " + self.token},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except OSError as exc:
            raise TransportError(str(exc)) from exc


def upstream_message(text: str, status: int | None) -> str:
    """A short, report-safe description of an upstream failure."""
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            text = str(payload.get("error") or payload.get("message") or text)
    except (ValueError, TypeError):
        pass
    text = " ".join(str(text).split())
    if status is not None:
        text = f"HTTP {status}: {text}" if text else f"HTTP {status}"
    return text[:400]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def make_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=None,
                    help="PostgreSQL DSN (default: $ML_WRITEBACK_DSN, then $ML_DSN)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="report plans without writing or sending (the default)")
    mode.add_argument("--apply", action="store_true",
                      help="claim, PATCH the upstream and settle each outcome")
    single = ap.add_mutually_exclusive_group()
    single.add_argument("--request-id", type=int, default=None,
                        help="restrict the batch to one request id")
    single.add_argument("--retry", type=int, default=None, metavar="N",
                        help="explicitly retry one known-failed request id")
    single.add_argument("--verify", type=int, default=None, metavar="N",
                        help="GET one unverifiable request and report the comparison")
    ap.add_argument("--resolve", action="store_true",
                    help="with --verify, let SQL resolve the request after a confirmed match")
    ap.add_argument("--limit", type=int, default=100,
                    help="maximum batch size (default 100)")
    ap.add_argument("--actor", default=None,
                    help="worker identity (default: $ML_JF_ACTOR or 'executor')")
    ap.add_argument("--json", dest="json_out", nargs="?", const="-", metavar="FILE",
                    help="write the full report to FILE, or print it as JSON to stdout")
    return ap


def _new_connection(dsn: str):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def _one(cur, sql: str, params=()):
    cur.execute(sql, params)
    row = cur.fetchone()
    return list(row.values())[0] if len(row) == 1 else row


def _claimable_ids(cur, only_id: int | None, limit: int) -> list[int]:
    cur.execute(
        "SELECT r.id FROM material_change_request r "
        " WHERE r.status = 'approved' "
        "   AND (r.claimed_at IS NULL OR r.claimed_at < now() - interval '5 minutes') "
        "   AND (%s::bigint IS NULL OR r.id = %s::bigint) "
        " ORDER BY r.requested_at, r.id LIMIT %s",
        (only_id, only_id, limit),
    )
    return [row["id"] for row in cur.fetchall()]


def _settle(cur, rid: int, outcome: str, status: int | None, error: str | None,
            actor: str) -> dict:
    return _one(
        cur,
        "SELECT material_change_request_settle(%s, %s, %s, %s, %s)",
        (rid, outcome, status, error, actor),
    )


def _blank_report(cfg: Config, actor: str) -> dict:
    return {
        "ok": True, "mode": cfg.mode, "checked": 0, "claimed": 0,
        "applied": 0, "failed": 0, "unknown": 0, "skipped": 0,
        "applied_unverified": 0, "rows": [],
        "workspace": cfg.workspace, "actor": actor,
    }


def _count(report: dict, outcome: str) -> None:
    if outcome == "applied":
        report["applied"] += 1
    elif outcome == "applied_unverified":
        report["failed"] += 1
        report["applied_unverified"] += 1
    elif outcome == "unknown":
        report["unknown"] += 1
    else:
        report["failed"] += 1


def dry_run(cur, cfg: Config, args, report: dict) -> None:
    ids = _claimable_ids(cur, args.request_id, args.limit)
    report["checked"] = len(ids)
    for rid in ids:
        plan = _one(cur, "SELECT material_change_request_plan(%s, %s)",
                    (rid, cfg.workspace))
        if not plan.get("ok"):
            report["skipped"] += 1
            report["rows"].append({
                "id": rid, "status": "skipped", "url": None,
                "headers": None, "body": None, "http_status": None,
                "error": plan.get("code"), "outcome": None,
            })
            continue
        # plan.headers deliberately has no Authorization: the token is never
        # written to a report (docs section 9).
        report["rows"].append({
            "id": rid, "status": "preview", "url": plan["url"],
            "headers": plan["headers"], "body": plan["body"],
            "http_status": None, "error": None, "outcome": "preview",
        })


def apply_batch(cur, cfg: Config, args, report: dict, client) -> None:
    claim = _one(
        cur,
        "SELECT material_change_request_claim(%s, %s, %s::interval, %s::bigint)",
        (args.limit, report["actor"], DEFAULT_LEASE, args.request_id),
    )
    report["claimed"] = claim["claimed"]
    report["checked"] = claim["claimed"]

    for item in claim["rows"]:
        rid = item["id"]
        plan = _one(cur, "SELECT material_change_request_plan(%s, %s)",
                    (rid, cfg.workspace))
        if not plan.get("ok"):
            settled = _settle(cur, rid, "failed", None,
                              plan.get("code", "invalid_target"), report["actor"])
            _count(report, settled["outcome"])
            report["skipped"] += 1
            report["rows"].append({
                "id": rid, "status": settled["status"], "url": None,
                "http_status": None, "error": settled["error"],
                "outcome": settled["outcome"],
            })
            continue

        try:
            status, text = client.patch(plan["url"], plan["headers"], plan["body"])
        except TransportError as exc:
            status, text, outcome = None, str(exc), "unknown"
        else:
            if 200 <= status < 300:
                outcome = "applied"
            elif 400 <= status < 500:
                outcome = "failed"
            else:
                outcome = "unknown"

        error = None if outcome == "applied" else upstream_message(text, status)
        settled = _settle(cur, rid, outcome, status, error, report["actor"])
        _count(report, settled["outcome"])
        report["rows"].append({
            "id": rid, "status": settled["status"], "url": plan["url"],
            "http_status": status, "error": settled["error"],
            "outcome": settled["outcome"],
        })


def same_value(left, right) -> bool:
    """Compare an upstream JSON value with the diff's `after` value."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, (list, dict)) or isinstance(right, (list, dict)):
        return (json.dumps(left, sort_keys=True, default=str)
                == json.dumps(right, sort_keys=True, default=str))
    return left == right


def verification_verdict(expected: dict, text: str, status: int | None) -> str:
    """Strict: a missing column means we do not know, never 'not applied'."""
    if status is None or not 200 <= status < 300:
        return "unreachable"
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return "unreachable"
    if not isinstance(payload, dict) or any(key not in payload for key in expected):
        return "unreachable"
    for key, want in expected.items():
        if not same_value(payload[key], want):
            return "upstream_differs"
    return "upstream_matches"


def verify_request(cur, cfg: Config, args, report: dict, client) -> None:
    """GET one unverifiable request, then let SQL decide (docs 6.4)."""
    rid = args.verify
    plan = _one(cur, "SELECT material_change_request_verify_plan(%s)", (rid,))
    report["checked"] = 1
    if not plan.get("ok"):
        # Not a candidate (already terminal, wrong target, ...): nothing to
        # verify and no HTTP call, so this is a skip rather than a failure.
        report["skipped"] += 1
        report["rows"].append({
            "id": rid, "status": "skipped", "url": None, "http_status": None,
            "error": plan.get("code"), "outcome": None, "verdict": None,
            "resolved": False,
        })
        return

    try:
        status, text = client.get(plan["url"])
    except TransportError:
        status, text, verdict = None, "", "unreachable"
    else:
        verdict = verification_verdict(plan["expected"], text, status)

    result = _one(
        cur,
        "SELECT material_change_request_verify(%s, %s, %s, %s)",
        (rid, verdict, report["actor"], args.resolve),
    )
    if not result.get("ok"):
        # A conflict means the row moved between plan and verify (someone else
        # resolved it); that is a skip, not a failure of this run.
        if result.get("code") == "conflict":
            report["skipped"] += 1
        else:
            report["failed"] += 1
    if result.get("code") == "applied":
        report["applied"] += 1
    report["rows"].append({
        "id": rid, "status": result.get("status", "unchanged"),
        "url": plan["url"], "http_status": status,
        "error": None if result.get("ok") else result.get("code"),
        "outcome": result.get("code"), "verdict": verdict,
        "resolved": bool(args.resolve),
    })


def run(args: argparse.Namespace, cfg: Config, dsn: str, *,
        connect=None, client=None) -> tuple[dict, int]:
    actor = args.actor or os.environ.get("ML_JF_ACTOR", "").strip() or DEFAULT_ACTOR
    report = _blank_report(cfg, actor)
    if cfg.mode in ("apply", "verify") and client is None:
        client = JoineryflowClient(cfg.base_url, cfg.token, cfg.timeout)

    conn = connect(dsn) if connect is not None else _new_connection(dsn)
    try:
        with conn.cursor() as cur:
            if cfg.mode == "retry":
                result = _one(
                    cur, "SELECT material_change_request_retry(%s, %s, %s)",
                    (args.retry, actor, None),
                )
                report["checked"] = 1
                if not result.get("ok"):
                    report["failed"] += 1
                report["rows"].append({
                    "id": args.retry, "status": result.get("status", "unchanged"),
                    "url": None, "http_status": None,
                    "error": None if result.get("ok") else result.get("code"),
                    "outcome": result.get("code"),
                })
            elif cfg.mode == "dry-run":
                dry_run(cur, cfg, args, report)
            elif cfg.mode == "verify":
                verify_request(cur, cfg, args, report, client)
            else:
                apply_batch(cur, cfg, args, report, client)
    finally:
        close = getattr(conn, "close", None)
        if close is not None:
            close()

    report["ok"] = report["failed"] == 0 and report["unknown"] == 0
    return report, 0 if report["ok"] else FAILURE


def emit(report: dict, args: argparse.Namespace) -> None:
    payload = json.dumps(report, sort_keys=True, indent=2, default=str) + "\n"
    if args.json_out == "-":
        print(payload, end="")
        return
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
    print(f"{'OK' if report['ok'] else 'FAIL'} mode={report['mode']} "
          f"checked={report['checked']} claimed={report['claimed']} "
          f"applied={report['applied']} failed={report['failed']} "
          f"unknown={report['unknown']} skipped={report['skipped']}")
    for row in report["rows"]:
        verdict = f" verdict={row['verdict']}" if "verdict" in row else ""
        print(f"  - id={row['id']} status={row['status']} url={row['url']} "
              f"http={row['http_status']} outcome={row['outcome']} "
              f"error={row['error']}{verdict}")
    if args.json_out:
        print(f"report -> {args.json_out}")


def main(argv: list[str] | None = None, *, connect=None, client=None) -> int:
    args = make_parser().parse_args(argv)
    if args.limit < 1 or args.limit > 500:
        print("error: --limit must be between 1 and 500", file=sys.stderr)
        return USAGE
    if args.retry and args.apply:
        print("error: --retry cannot be combined with --apply", file=sys.stderr)
        return USAGE
    if args.resolve and args.verify is None:
        print("error: --resolve requires --verify N", file=sys.stderr)
        return USAGE
    if args.verify is not None and (args.apply or args.dry_run):
        # --verify makes a real GET, so it must not be reachable from a command
        # a reader would take as read-only.
        print("error: --verify cannot be combined with --apply or --dry-run",
              file=sys.stderr)
        return USAGE
    try:
        cfg = build_config(args)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return USAGE

    dsn = args.dsn or os.environ.get("ML_WRITEBACK_DSN") or os.environ.get("ML_DSN")
    if not dsn:
        print("error: a worker needs --dsn (or $ML_WRITEBACK_DSN / $ML_DSN)",
              file=sys.stderr)
        return USAGE
    try:
        report, code = run(args, cfg, dsn, connect=connect, client=client)
    except Exception as exc:                    # database failures, bad SQL, ...
        print(f"error: {exc}", file=sys.stderr)
        return FAILURE
    emit(report, args)
    return code


if __name__ == "__main__":
    sys.exit(main())
