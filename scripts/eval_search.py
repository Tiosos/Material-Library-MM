"""Evaluate search quality against eval/golden.yaml.

Usage:
    python scripts/eval_search.py --dsn postgresql://... --dataset fixture
    python scripts/eval_search.py --dsn ... --dataset blum --json .tmp/eval_blum.json

Exit code 1 when a case asserts something false or a holdout threshold misses, 0 otherwise.
The SQL contract is exercised, not reimplemented: every case is one call to material_search(),
which is the same function the HTTP layer calls.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import psycopg
import yaml

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval" / "golden.yaml"

# One page of ten results is what MRR@10 and hit@1 are defined against (docs/api-v1.md 6.2).
LIMIT = 10

SEARCH_SQL = """
    SELECT material_uid, score, level, total
      FROM material_search(
               p_q => %(q)s,
               p_category => %(category)s,
               p_brand => %(brand)s,
               p_supplier => %(supplier)s,
               p_attrs => %(attrs)s::jsonb,
               p_limit => %(limit)s,
               p_measures => %(measures)s::jsonb)
     ORDER BY score DESC, material_uid
"""

LEVELS = {"exact", "prefix", "and", "relaxed", "fuzzy", "browse", "none"}


class CaseError(Exception):
    """Raised when a case is malformed in the golden file (a harness bug, not a search bug)."""


def load_golden(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict) or "datasets" not in data:
        raise CaseError(f"{path}: expected a mapping with a 'datasets' key")
    return data


def case_query(case: dict[str, Any]) -> dict[str, Any]:
    for key in ("expect_level", "id", "split"):
        if key not in case:
            raise CaseError(f"case {case.get('id', '?')} is missing {key!r}")
    if case["split"] not in {"tuning", "holdout"}:
        raise CaseError(f"case {case['id']}: split must be tuning or holdout")
    if case["expect_level"] not in LEVELS:
        raise CaseError(f"case {case['id']}: unknown expect_level {case['expect_level']!r}")
    if not any(k in case for k in ("expect_any", "expect_top1", "expect_min_hits", "expect_total")):
        raise CaseError(f"case {case['id']}: asserts nothing")
    return {
        "q": case.get("q"),
        "category": case.get("category"),
        "brand": case.get("brand"),
        "supplier": case.get("supplier"),
        "attrs": json.dumps(case.get("attrs") or {}, sort_keys=True),
        "measures": json.dumps(case.get("measures") or {}, sort_keys=True),
        "limit": LIMIT,
    }


def expected_relevant(case: dict[str, Any]) -> set[str]:
    relevant = set(case.get("expect_any") or [])
    if case.get("expect_top1"):
        relevant.add(case["expect_top1"])
    return relevant


def evaluate_case(conn: psycopg.Connection, case: dict[str, Any]) -> dict[str, Any]:
    params = case_query(case)
    rows = conn.execute(SEARCH_SQL, params).fetchall()
    uids = [row[0] for row in rows]
    level = rows[0][2] if rows else "none"
    total = int(rows[0][3]) if rows else 0
    relevant = expected_relevant(case)

    failures: list[str] = []

    if level != case["expect_level"]:
        failures.append(f"level {level!r} != expected {case['expect_level']!r}")

    if "expect_total" in case and total != int(case["expect_total"]):
        failures.append(f"total {total} != expected {case['expect_total']}")

    if "expect_min_hits" in case and total < int(case["expect_min_hits"]):
        failures.append(f"total {total} < expected minimum {case['expect_min_hits']}")

    if case.get("expect_top1") and (not uids or uids[0] != case["expect_top1"]):
        top = uids[0] if uids else None
        failures.append(f"top1 {top} != expected {case['expect_top1']}")

    rank = next((index + 1 for index, uid in enumerate(uids) if uid in relevant), None) if relevant else None
    if relevant and rank is None:
        failures.append(f"none of the {len(relevant)} relevant uids is in the top {LIMIT}")

    return {
        "id": case["id"],
        "split": case["split"],
        "passed": not failures,
        "failures": failures,
        "level": level,
        "total": total,
        "rank": rank,
        "top": uids[:3],
        "note": (case.get("note") or "").strip(),
    }


def summarise(results: list[dict[str, Any]], thresholds: dict[str, float], split: str) -> dict[str, Any]:
    cases = [result for result in results if result["split"] == split]
    scoring = [result for result in cases if result["rank"] is not None]

    # A positive case is one whose contract is "something is found": any case with a relevant
    # uid listed, or a case whose only assertion is a count that is not zero. Negative cases
    # (expect_total: 0) are counted separately so they cannot inflate the zero-result ratio.
    positive = [
        result for result in cases if result["rank"] is not None or result["total"] > 0
    ]
    negative = [result for result in cases if result["total"] == 0 and result["level"] == "none"]

    mrr = (sum(1.0 / result["rank"] for result in scoring) / len(scoring)) if scoring else 0.0
    hit_at_1 = (
        sum(1 for result in scoring if result["rank"] == 1) / len(scoring) if scoring else 0.0
    )
    zero = sum(1 for result in positive if result["total"] == 0)
    zero_rate = (zero / len(positive)) if positive else 0.0

    return {
        "cases": len(cases),
        "scoring_cases": len(scoring),
        "positive_cases": len(positive),
        "negative_cases": len(negative),
        "mrr_at_10": round(mrr, 4),
        "hit_at_1": round(hit_at_1, 4),
        "zero_result_rate": round(zero_rate, 4),
        "thresholds_apply": split == "holdout",
    }


def check_thresholds(summary: dict[str, Any], thresholds: dict[str, float]) -> list[str]:
    missed: list[str] = []
    if summary["mrr_at_10"] < thresholds["mrr_at_10"]:
        missed.append(f"MRR@10 {summary['mrr_at_10']:.4f} < {thresholds['mrr_at_10']:.2f}")
    if summary["hit_at_1"] < thresholds["hit_at_1"]:
        missed.append(f"hit@1 {summary['hit_at_1']:.4f} < {thresholds['hit_at_1']:.2f}")
    if summary["zero_result_rate"] > thresholds["zero_result_max"]:
        missed.append(
            f"zero-result rate {summary['zero_result_rate']:.2%} > {thresholds['zero_result_max']:.0%}"
        )
    return missed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True, help="PostgreSQL DSN, e.g. postgresql://.../ml_eval")
    parser.add_argument("--dataset", required=True, choices=sorted(load_golden(GOLDEN)["datasets"]))
    parser.add_argument("--golden", default=str(GOLDEN), help="path to the case file")
    parser.add_argument("--json", dest="json_path", help="write the full result as JSON here")
    parser.add_argument("--quiet", action="store_true", help="only print failures and the summary")
    args = parser.parse_args(argv)

    golden_path = Path(args.golden)
    golden = load_golden(golden_path)
    thresholds = golden["thresholds"]
    cases = golden["datasets"][args.dataset]["cases"]

    with psycopg.connect(args.dsn) as conn:
        material_count = conn.execute("SELECT count(*) FROM material").fetchone()[0]
        results = []
        for case in cases:
            try:
                results.append(evaluate_case(conn, case))
            except psycopg.Error as exc:
                raise SystemExit(f"FAIL {case.get('id', '?')}: {type(exc).__name__}: {exc}") from exc

    level_failures = [r for r in results if any(f.startswith("level ") for f in r["failures"])]
    level_accuracy = 1.0 - len(level_failures) / len(results) if results else 1.0

    holdout = summarise(results, thresholds, "holdout")
    tuning = summarise(results, thresholds, "tuning")
    missed = check_thresholds(holdout, thresholds) if holdout["cases"] else []

    if level_accuracy < thresholds["level_accuracy"]:
        missed.insert(0, f"level accuracy {level_accuracy:.2%} < {thresholds['level_accuracy']:.0%}")

    if not args.quiet:
        print(f"dataset   {args.dataset} ({material_count} rows in the catalogue)")
        print(f"golden    {golden_path} (version {golden.get('version')})")
        for result in results:
            status = "ok  " if result["passed"] else "FAIL"
            print(f"  {status} {result['id']:<28} {result['split']:<7} level={result['level']:<7} total={result['total']:>3}")
            for failure in result["failures"]:
                print(f"         - {failure}")

    for name, summary in (("holdout", holdout), ("tuning", tuning)):
        print(
            f"{name:<7} cases={summary['cases']:<3} scoring={summary['scoring_cases']:<3} "
            f"MRR@10={summary['mrr_at_10']:.3f} hit@1={summary['hit_at_1']:.3f} "
            f"zero-result={summary['zero_result_rate']:.1%} level-accuracy={level_accuracy:.1%}"
        )

    for miss in missed:
        print(f"THRESHOLD MISSED: {miss}")

    failures = [r for r in results if not r["passed"]]
    payload = {
        "dataset": args.dataset,
        "catalogue_rows": material_count,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "golden_version": golden.get("version"),
        "thresholds": thresholds,
        "level_accuracy": round(level_accuracy, 4),
        "holdout": holdout,
        "tuning": tuning,
        "threshold_failures": missed,
        "failed_cases": [r["id"] for r in failures],
        "cases": results,
        "passed": not failures and not missed,
    }
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"json      {args.json_path}")

    if not payload["passed"]:
        print(f"RESULT    FAIL ({len(failures)} case(s), {len(missed)} threshold(s))")
        return 1
    print("RESULT    PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
