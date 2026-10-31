#!/usr/bin/env python3
"""Graceful Degradation Search — prototype implementation.

Four-level degradation chain (corresponds to chapter 5 of docs/design-v1.md):

    L0 exact   exact code hit (a direct identifier hit)
    L1 prefix  prefix / weighted full-text AND — every term must appear
    L2 relaxed automatically relaxes to OR — ranked by "how many terms matched" (the heart of default search)
    L3 fuzzy   trigram/edit-distance tolerance + synonym expansion + spelling suggestion

Stops as soon as a level returns results, and reports which level was used so the UI can tell the user.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "materials.sqlite"

# bm25 column weights: ids > name > brand > dims > blob
W = (12.0, 8.0, 5.0, 2.0, 1.0)

LEVEL_LABELS = {
    "exact": "Exact code match",
    "and": "Full-text match (all keywords)",
    "prefix": "Prefix match (works while typing)",
    "relaxed": "Relaxed (some keywords)",
    "filter": "Filtered by attribute",
    "fuzzy": "Fuzzy match (possible typo)",
    "none": "No match",
    "browse": "Browsing all",
}

_PHRASE = re.compile(r'"([^"]+)"')
_ASCII = re.compile(r"^[A-Za-z0-9.\-_/]+$")


@dataclass
class Query:
    raw: str
    terms: list[str] = field(default_factory=list)     # plain terms
    phrases: list[str] = field(default_factory=list)   # "quoted phrases"
    excludes: list[str] = field(default_factory=list)  # -excluded terms
    filters: dict[str, str] = field(default_factory=dict)  # key:value


def parse_query(raw: str) -> Query:
    q = Query(raw=(raw or "").strip())
    s = q.raw
    for m in _PHRASE.finditer(s):
        q.phrases.append(m.group(1).strip())
    s = _PHRASE.sub(" ", s)
    for tok in s.split():
        if tok.startswith("-") and len(tok) > 1:
            q.excludes.append(tok[1:])
        elif ":" in tok and not tok.startswith("http"):
            k, _, v = tok.partition(":")
            if v:
                q.filters[k.lower()] = v
        else:
            q.terms.append(tok)
    return q


def _esc(term: str) -> str:
    """FTS5 term escaping: wrap in double quotes and double any inner double quote."""
    return '"' + term.replace('"', '""') + '"'


def _fts_and(q: Query) -> str | None:
    parts = [_esc(t) for t in q.terms] + [_esc(p) for p in q.phrases]
    if not parts:
        return None
    s = " AND ".join(parts)
    for ex in q.excludes:
        s += " NOT " + _esc(ex)
    return s


def _fts_or(q: Query) -> str | None:
    parts = [_esc(t) for t in q.terms] + [_esc(p) for p in q.phrases]
    if not parts:
        return None
    s = " OR ".join(parts)
    for ex in q.excludes:
        s += " NOT " + _esc(ex)
    return s


def _fts_prefix(q: Query) -> str | None:
    parts = [_esc(t) + "*" for t in q.terms]
    if not parts:
        return None
    return " AND ".join(parts)


# --------------------------------------------------------------------------- #
# Main search
# --------------------------------------------------------------------------- #
def search(raw: str, category: str = "", brand: str = "", supplier: str = "",
           status: str = "active", limit: int = 40, include_archived: bool = True) -> dict:
    q = parse_query(raw)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    def cat_clause(alias: str = "m") -> tuple[str, list]:
        clauses, params = [], []
        if category:
            clauses.append(f"{alias}.category = ?"); params.append(category)
        if brand:
            clauses.append(f"{alias}.brand = ?"); params.append(brand)
        if supplier:
            clauses.append(f"{alias}.default_supplier = ?"); params.append(supplier)
        if status == "active" and not include_archived:
            clauses.append(f"{alias}.status = 'active'")
        return (" AND ".join(clauses), params) if clauses else ("1=1", [])

    def hydrate(rows: list[sqlite3.Row]) -> list[dict]:
        out = []
        for r in rows:
            d = dict(r)
            d["attributes"] = json.loads(d.get("attributes") or "{}")
            d["synonyms"] = json.loads(d.get("synonyms") or "[]")
            d["relations"] = json.loads(d.get("relations") or "[]")
            d["prices"] = json.loads(d.get("prices") or "[]")
            d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
            out.append(d)
        return out

    results: list[dict] = []
    level = "browse"

    # ---------------- F: attribute filters only, no keywords (thickness:18 colour:black) ------- #
    if not q.terms and not q.phrases and q.filters:
        where, ps = cat_clause()
        rows = con.execute(
            f"SELECT m.*, 0 AS score FROM material m WHERE {where} "
            f"ORDER BY m.ref_count DESC, m.name LIMIT ?", [*ps, 2000]
        ).fetchall()
        results = hydrate(rows)
        level = "filter"

    # ---------------- L0: exact code hit ---------------- #
    if q.terms or q.phrases:
        probe = (q.phrases + q.terms)[0]
        where, ps = cat_clause()
        rows = con.execute(
            f"""SELECT m.*, 1 AS score FROM material m
                JOIN material_identifier i ON i.material_uid = m.material_uid
                WHERE (lower(i.value) = lower(?) OR lower(i.value) LIKE lower(?) || '%')
                  AND {where} LIMIT ?""",
            [probe, probe, *ps, limit],
        ).fetchall()
        if rows:
            results = hydrate(rows)
            level = "exact"

    # ---------------- L1: weighted full-text AND ---------------- #
    if not results:
        match = _fts_and(q)
        if match:
            where, ps = cat_clause()
            sql = f"""
                SELECT m.*, (0 - bm25(material_fts, {W[0]}, {W[1]}, {W[2]}, {W[3]}, {W[4]})) AS score
                FROM material_fts f JOIN material m ON m.material_uid = f.material_uid
                WHERE material_fts MATCH ? AND {where}
                ORDER BY score DESC, m.ref_count DESC LIMIT ?"""
            try:
                rows = con.execute(sql, [match, *ps, limit]).fetchall()
            except sqlite3.Error:
                rows = []
            if rows:
                results = hydrate(rows)
                level = "and"

    # ---------------- L1.5: prefix relaxation (half-typed) ---------------- #
    if not results:
        match = _fts_prefix(q)
        if match:
            where, ps = cat_clause()
            sql = f"""
                SELECT m.*, (0 - bm25(material_fts, {W[0]}, {W[1]}, {W[2]}, {W[3]}, {W[4]})) AS score
                FROM material_fts f JOIN material m ON m.material_uid = f.material_uid
                WHERE material_fts MATCH ? AND {where}
                ORDER BY score DESC, m.ref_count DESC LIMIT ?"""
            try:
                rows = con.execute(sql, [match, *ps, limit]).fetchall()
            except sqlite3.Error:
                rows = []
            if rows:
                results = hydrate(rows)
                level = "and"

    # ---------------- L2: drop terms one level at a time (precision first: drop the terms that did not match, then AND) ---------------- #
    if not results and len(q.terms) > 1:
        for drop in range(1, len(q.terms)):
            sub = Query(raw=q.raw, terms=q.terms[:-drop], phrases=q.phrases, excludes=q.excludes)
            match = _fts_and(sub)
            if not match:
                continue
            where, ps = cat_clause()
            sql = f"""
                SELECT m.*, (0 - bm25(material_fts, {W[0]}, {W[1]}, {W[2]}, {W[3]}, {W[4]})) AS score
                FROM material_fts f JOIN material m ON m.material_uid = f.material_uid
                WHERE material_fts MATCH ? AND {where}
                ORDER BY score DESC, m.ref_count DESC LIMIT ?"""
            try:
                rows = con.execute(sql, [match, *ps, limit]).fetchall()
            except sqlite3.Error:
                rows = []
            if rows:
                results = hydrate(rows)
                level = "relaxed"
                break

    # ---------------- L2.5: relax to OR (recall safety net) ---------------- #
    if not results:
        match = _fts_or(q)
        if match and len(q.terms) + len(q.phrases) > 1:
            where, ps = cat_clause()
            sql = f"""
                SELECT m.*, (0 - bm25(material_fts, {W[0]}, {W[1]}, {W[2]}, {W[3]}, {W[4]})) AS score
                FROM material_fts f JOIN material m ON m.material_uid = f.material_uid
                WHERE material_fts MATCH ? AND {where}
                ORDER BY score DESC, m.ref_count DESC LIMIT ?"""
            try:
                rows = con.execute(sql, [match, *ps, limit]).fetchall()
            except sqlite3.Error:
                rows = []
            if rows:
                results = hydrate(rows)
                level = "relaxed"

    # ---------------- L3: fuzzy tolerance (word-level similarity, not whole-string comparison) ---------------- #
    if not results and (q.terms or q.phrases):
        probe = (q.terms + q.phrases)[0].lower()
        where, ps = cat_clause()
        # (a) substring: catches untokenised scripts (FTS5 unicode61 does not split CJK words)
        rows = con.execute(
            f"""SELECT m.*, 0.5 AS score FROM material m
                WHERE (lower(m.name) LIKE '%' || lower(?) || '%'
                       OR lower(m.synonyms) LIKE '%' || lower(?) || '%'
                       OR lower(m.brand) LIKE '%' || lower(?) || '%')
                  AND {where} LIMIT ?""",
            [probe, probe, probe, *ps, limit],
        ).fetchall()
        if rows:
            results = hydrate(rows)
            level = "fuzzy"
        else:
            # (b) word-level edit distance: compare probe with each candidate's "word bag", not with the whole string
            #     "luminux" vs the full name "16mm White Melamine MDF" scores very low,
            #     vs the single word "Laminex" it is meaningful
            allrows = con.execute(f"SELECT * FROM material m WHERE {where}", ps).fetchall()
            scored = []
            for r in allrows:
                best, hit = 0.0, ""
                for tok in _wordbag(r):
                    ratio = difflib.SequenceMatcher(None, probe, tok).ratio()
                    if ratio > best:
                        best, hit = ratio, tok
                if best >= 0.68:
                    d = dict(r)
                    d["score"] = round(best, 3)
                    d["_matched"] = hit
                    scored.append(d)
            scored.sort(key=lambda d: -d["score"])
            if scored:
                results = scored[:limit]
                level = "fuzzy"

    # ---------------- Empty query: browse; query with no hits: say "no match" outright ---------------- #
    if not results and not q.raw:
        where, ps = cat_clause()
        rows = con.execute(
            f"SELECT m.*, 0 AS score FROM material m WHERE {where} "
            f"ORDER BY m.ref_count DESC, m.name LIMIT ?", [*ps, limit]
        ).fetchall()
        results = hydrate(rows)
        level = "browse"
    elif not results:
        level = "none"

    # ---------------- Attribute filtering (thickness:18 / colour:black) ---------------- #
    if q.filters:
        kept = []
        for r in results:
            ok = True
            for k, v in q.filters.items():
                akey = "thickness_mm" if k in ("thickness", "thickness_mm") else k
                val = r["attributes"].get(akey)
                if akey == "thickness_mm":
                    ok = val is not None and abs(float(val) - float(v)) < 0.51
                elif val is None:
                    ok = False
                else:
                    ok = str(val).lower() == v.lower() or v.lower() in str(val).lower()
                if not ok:
                    break
            if ok:
                kept.append(r)
        # Note: once attribute filtering is used, follow the filtered result even if empty —
        # otherwise the unfiltered list would be mistaken for the result
        results = kept

    # ---------------- Unified deserialisation (the fuzzy branch works on plain dicts) ---------------- #
    for r in results:
        for k in ("attributes", "synonyms", "relations", "prices"):
            if isinstance(r.get(k), str):
                r[k] = json.loads(r[k] or ("{}" if k == "attributes" else "[]"))
        if isinstance(r.get("tags"), str):
            r["tags"] = [t for t in r["tags"].split(",") if t]

    # ---------------- Ranking weights: relevance + popularity - archived penalty ---------------- #
    maxref = max([r["ref_count"] for r in results] or [1]) or 1
    maxscore = max([r.get("score") or 0 for r in results] or [1]) or 1
    for r in results:
        # The fuzzy branch score is already a 0~1 similarity; the FTS branch is bm25, normalised within the batch
        rel = (r.get("score") or 0) if level == "fuzzy" else (r.get("score") or 0) / maxscore
        rel = min(1.0, rel)
        pop = (r["ref_count"] or 0) / maxref
        pen = 0.35 if r["status"] == "archived" else 0.0
        r["final_score"] = round(max(0.0, 0.70 * rel + 0.30 * pop - pen), 4)
    results.sort(key=lambda r: -r["final_score"])

    # ---------------- Facets ---------------- #
    facets = {}
    for key in ("category", "brand", "default_supplier", "unit"):
        facets[key] = {
            k: v for k, v in con.execute(
                f"SELECT {key}, COUNT(*) FROM material WHERE {key} IS NOT NULL "
                f"GROUP BY {key} ORDER BY COUNT(*) DESC LIMIT 12"
            ).fetchall() if k
        }

    # ---------------- Spelling suggestion (use the word bag, not the whole string) ---------------- #
    suggestion = None
    if level in ("fuzzy", "none") and q.raw:
        freq: dict[str, int] = {}
        for row in con.execute("SELECT * FROM material").fetchall():
            for t in set(_wordbag(row)):
                freq[t] = freq.get(t, 0) + 1
        cands = difflib.get_close_matches(q.raw.lower(), list(freq), n=6, cutoff=0.60)
        cands = [c for c in cands if c != q.raw.lower()]
        # Prefer the word with more materials behind it, so a typo is not corrected to an obscure record
        if cands:
            suggestion = max(cands, key=lambda c: (freq[c], difflib.SequenceMatcher(
                None, q.raw.lower(), c).ratio()))

    con.close()
    return {
        "q": q.raw,
        "level": level,
        "level_label": LEVEL_LABELS.get(level, level),
        "parsed": {"terms": q.terms, "phrases": q.phrases, "excludes": q.excludes, "filters": q.filters},
        "total": len(results),
        "results": results[:limit],
        "facets": facets,
        "suggestion": suggestion,
    }


def _ascii_only(s: str) -> bool:
    return bool(_ASCII.match(s))


_WORD = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")


def _wordbag(row: sqlite3.Row) -> list[str]:
    """The comparable word bag for one material: name/brand/type/category/synonyms, each split into words."""
    toks: set[str] = set()
    for col in ("name", "brand", "product_type", "category", "default_supplier"):
        val = row[col] if col in row.keys() else None
        for t in _WORD.split((val or "").lower()):
            if len(t) >= 3:
                toks.add(t)
    raw = row["synonyms"] if "synonyms" in row.keys() else "[]"
    syns = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
    for s in syns:
        t = str(s).strip().lower()
        if len(t) >= 2:
            toks.add(t)
    return list(toks)


def get_one(uid: str) -> dict | None:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM material WHERE material_uid = ?", [uid]).fetchone()
    if not r:
        con.close()
        return None
    d = dict(r)
    d["attributes"] = json.loads(d["attributes"] or "{}")
    d["synonyms"] = json.loads(d["synonyms"] or "[]")
    d["relations"] = json.loads(d["relations"] or "[]")
    d["prices"] = json.loads(d["prices"] or "[]")
    d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
    d["identifiers"] = [dict(x) for x in con.execute(
        "SELECT kind, value, source_system, is_primary FROM material_identifier WHERE material_uid = ?", [uid]
    ).fetchall()]
    for rel in d["relations"]:
        t = con.execute("SELECT material_uid, name, category FROM material WHERE material_uid = ?",
                        [rel["to_uid"]]).fetchone()
        rel["to_name"] = t["name"] if t else None
        rel["to_category"] = t["category"] if t else None
    con.close()
    return d


if __name__ == "__main__":
    import sys
    for term in (sys.argv[1:] or ["blum hinge", "blu", "16mm black", "luminux", "mdf", "countertop"]):
        r = search(term)
        print(f"\n=== {term!r}  →  {r['level']} ({r['level_label']})  {r['total']} results")
        for x in r["results"][:5]:
            print(f"   {x['final_score']:.3f}  [{x['category']:10}] {x['name'][:52]:54} ${x['unit_cost']}")
        if r["suggestion"]:
            print(f"   Did you mean: {r['suggestion']}")
