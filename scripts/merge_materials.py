#!/usr/bin/env python
"""Merge one duplicate pair from the command line (docs/t2-merge.md 8).

For environments without the HTTP edge. It calls the same SQL function the API calls, so the
outcome is identical: the keeper wins its fields, the survivor becomes a superseded tombstone,
the children move, and the decision is recorded on the candidate row.

    python scripts/merge_materials.py --dsn ... --keep UID --merge UID --reason TEXT [--dry-run]

Exit codes: 0 merged or planned, 1 the database refused (conflict, not found), 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge one duplicate material pair.")
    parser.add_argument("--dsn", default=os.environ.get("ML_DSN"), help="PostgreSQL DSN")
    parser.add_argument("--keep", required=True, help="material_uid to keep")
    parser.add_argument("--merge", required=True, dest="merge_uid", help="material_uid to supersede")
    parser.add_argument("--reason", default=None, help="why the pair is the same material")
    parser.add_argument("--actor", default=os.environ.get("ML_ACTOR_DEFAULT", "cli"), help="who decided")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    parser.add_argument("--json", default=None, help="write the report to this file")
    return parser.parse_args(argv)


def summarise(report: dict) -> str:
    """One screenful: what happened, what moved, what needs a human eye."""
    if report.get("code") in {"not_found", "conflict"}:
        blockers = report.get("blockers") or []
        return f"{report['code']}: {', '.join(blockers) if blockers else 'see the plan'}"
    lines = [f"{report['code']}: keep {report['keep']}, merge {report['merged']}"]
    plan = report.get("plan") or {}
    fields = plan.get("fields") or {}
    if fields:
        lines.append(f"  fields adopted: {', '.join(sorted(fields))}")
    dropped = plan.get("dropped") or []
    if dropped:
        names = sorted(
            entry.get("field", str(entry)) if isinstance(entry, dict) else str(entry)
            for entry in dropped
        )
        lines.append(f"  survivor values dropped: {', '.join(names)}")
    children = report.get("children") or plan.get("children") or {}
    moved = {k: v for k, v in children.items() if v}
    if moved:
        rendered = ", ".join(
            f"{key}={value if not isinstance(value, dict) else json.dumps(value, sort_keys=True)}"
            for key, value in moved.items()
        )
        lines.append(f"  children: {rendered}")
    warnings = report.get("warnings") or []
    for warning in warnings:
        codes = warning.get("code") if isinstance(warning, dict) else warning
        detail = warning.get("message", "") if isinstance(warning, dict) else ""
        lines.append(f"  warning: {codes}{(' - ' + detail) if detail else ''}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.dsn:
        print("ML_DSN is not set and --dsn was not given", file=sys.stderr)
        return 2
    if not args.reason:
        print("note: --reason is empty, the decision will be recorded without a comment")

    try:
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            report = conn.execute(
                """
                SELECT material_merge(
                    %(a)s::text, %(b)s::text, %(keep)s::text, %(reason)s::text,
                    %(dry_run)s::boolean, %(actor)s::text
                )
                """,
                {
                    "a": args.keep,
                    "b": args.merge_uid,
                    "keep": args.keep,
                    "reason": args.reason,
                    "dry_run": args.dry_run,
                    "actor": args.actor,
                },
            ).fetchone()[0]
    except psycopg.Error as exc:
        print(f"merge failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(summarise(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
    return 0 if report.get("code") in {"merged", "dry_run", "already_merged"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
