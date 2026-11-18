"""Pre-import data-quality signals for a batch of drafts.

The B9 design (docs/import-pipeline-b9.md section 6) calls for two signals in the import report:
a completeness distribution and suspected duplicates. Both reuse database objects that already
exist: `v_material_completeness` (its rule: a required field is present if it is in `attributes`
or in `material_measure`) and `material_duplicate_candidate` (open pairs found by T2's scanner).

Nothing here writes: the DB queries are SELECTs only, and the pure functions work on Draft objects
so the batch-level checks are testable without a database. `import_file.py` calls these in the
dry-run phase, before `apply()`, so a bad auto-inferred mapping can be caught before it upserts
existing materials.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from .importer import Draft

# Matches v_material_completeness: attributes OR a measure row with that key.
def draft_required_present(d: Draft, required: Iterable[str]) -> tuple[int, int]:
    keys = list(required)
    have = set(d.attributes) | {m["key"] for m in d.measures}
    present = sum(1 for k in keys if k in have)
    return present, len(keys)


def identifier_key(ident: dict) -> tuple[str, str, str]:
    return (ident["kind"], str(ident["value"]), ident["source_system"])


def within_batch_duplicates(drafts: list[Draft]) -> list[dict]:
    """Codes repeated inside the same file: each could overwrite another row on apply."""
    seen: dict[tuple[str, str, str], list[int]] = {}
    for d in drafts:
        for ident in d.identifiers:
            seen.setdefault(identifier_key(ident), []).append(d.row_no)
    out = []
    for (kind, value, source), rows in sorted(seen.items()):
        if len(rows) > 1:
            out.append({"kind": kind, "value": value, "source_system": source, "rows": rows})
    return out


def completeness_distribution(
    drafts: list[Draft], required_by_category: dict[str, list[str]] | None
) -> dict:
    """Bucket rows by required-field coverage. `None` means no DB profile context was loaded,
    so the check is skipped (and never gates --strict); `{}` means loaded but this category has
    no profile, which is a real 'unknown' signal."""
    if required_by_category is None:
        return {"checked": False, "buckets": {}, "unknown_category": 0,
                "incomplete_rows": [], "incomplete_count": 0}
    buckets: Counter[str] = Counter()
    detail = []
    for d in drafts:
        required = required_by_category.get(d.category_code)
        if required is None:
            buckets["unknown"] += 1
            detail.append({"row": d.row_no, "category_code": d.category_code,
                           "present": None, "required": None})
            continue
        present, total = draft_required_present(d, required)
        label = "full" if total and present == total else ("none" if present == 0 else "partial")
        buckets[label] += 1
        detail.append({"row": d.row_no, "category_code": d.category_code,
                       "present": present, "required": total})
    incomplete = [x for x in detail if x["present"] is not None and x["present"] < x["required"]]
    return {
        "checked": True,
        "buckets": dict(buckets),
        "unknown_category": buckets.get("unknown", 0),
        "incomplete_rows": incomplete[:20],
        "incomplete_count": len(incomplete),
    }


def assess(
    drafts: list[Draft],
    *,
    required_by_category: dict[str, list[str]] | None = None,
    existing_identifiers: set[tuple[str, str, str]] | None = None,
    open_duplicate_candidates: int | None = None,
) -> dict:
    """Assemble the quality section. Pure: callers supply whatever DB context they have."""
    valid = [d for d in drafts if not d.errors]
    collisions = []
    if existing_identifiers is not None:
        for d in valid:
            for ident in d.identifiers:
                if identifier_key(ident) in existing_identifiers:
                    collisions.append({"row": d.row_no, "value": ident["value"],
                                       "action": "update existing material"})
    report = {
        "rows_checked": len(valid),
        "within_batch_duplicates": within_batch_duplicates(valid),
        "completeness": completeness_distribution(valid, required_by_category),
        "identifier_collisions": collisions[:20],
        "collision_count": len(collisions),
        "open_duplicate_candidates": open_duplicate_candidates,
        "has_signals": False,
    }
    # Collisions with existing identifiers are NOT a signal: upsert-on-code is the designed
    # idempotency path, so a re-run would otherwise fail --strict forever. They are informational.
    # Only THIS batch's own problems gate --strict. DB-wide context is informational:
    # upsert-on-code collisions are the designed re-run path, and open T2 candidates are
    # historical state unrelated to this file. Counting either would block every re-run.
    report["has_signals"] = bool(
        report["within_batch_duplicates"]
        or report["completeness"]["incomplete_count"]
        or report["completeness"]["unknown_category"]
    )
    return report


# ---------------------------------------------------------------------------
# DB context (SELECT only)
# ---------------------------------------------------------------------------
def load_quality_context(conn, drafts: list[Draft]) -> dict:
    """Fetch the three DB-side inputs used by assess(). No writes."""
    categories = {d.category_code for d in drafts if not d.errors and d.category_code}
    required: dict[str, list[str]] = {}
    existing: set[tuple[str, str, str]] = set()
    with conn.cursor() as cur:
        if categories:
            cur.execute("SELECT category_code, required FROM material_profile WHERE category_code = ANY(%s)",
                        (list(categories),))
            required = {row[0]: list(row[1] or []) for row in cur.fetchall()}
        codes = list({(ident["kind"], str(ident["value"]), ident["source_system"])
                      for d in drafts if not d.errors for ident in d.identifiers})
        if codes:
            # One round trip: unnest three parallel arrays and match tuples via EXISTS.
            kinds = [c[0] for c in codes]
            values = [c[1] for c in codes]
            sources = [c[2] for c in codes]
            cur.execute(
                """SELECT w.kind, w.value, w.source_system
                     FROM unnest(%s::text[], %s::text[], %s::text[]) AS w(kind, value, source_system)
                    WHERE EXISTS (SELECT 1 FROM material_identifier mi
                                   WHERE mi.kind=w.kind AND mi.value=w.value
                                     AND mi.source_system=w.source_system)""",
                (kinds, values, sources),
            )
            existing = {(row[0], str(row[1]), row[2]) for row in cur.fetchall()}
        cur.execute("SELECT count(*) FROM material_duplicate_candidate WHERE status='open'")
        open_duplicates = cur.fetchone()[0]
    return {
        "required_by_category": required,
        "existing_identifiers": existing,
        "open_duplicate_candidates": open_duplicates,
    }


def print_quality(report: dict) -> None:
    """Human-readable quality section for the dry run."""
    print("quality:")
    c = report["completeness"]
    if c.get("checked"):
        print(f"  completeness: {c['buckets'] or 'n/a'} "
              f"(incomplete={c['incomplete_count']}, unknown category={c['unknown_category']})")
    else:
        print("  completeness: not checked (no DB profile context)")
    print(f"  identifier collisions: {report['collision_count']} "
          "(already in DB -> apply will UPDATE, not insert)")
    print(f"  within-batch duplicate codes: {len(report['within_batch_duplicates'])}")
    print(f"  open duplicate candidates in DB: {report['open_duplicate_candidates']}")
    for dup in report["within_batch_duplicates"][:10]:
        print(f"    duplicate {dup['value']!r} on rows {dup['rows']}")
    for row in c["incomplete_rows"][:10]:
        print(f"    row {row['row']}: {row['present']}/{row['required']} required fields ({row['category_code']})")
    if not report["has_signals"]:
        print("  no quality signals - batch looks clean")
