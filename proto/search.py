#!/usr/bin/env python3
"""缺省搜索（Graceful Degradation Search）—— 原型实现。

四级降级链（对应 docs/方案-v1.md 第 5 章）：

    L0 exact   编码精确命中（identifier 直接命中）
    L1 prefix  前缀/加权全文 AND —— 所有词都必须出现
    L2 relaxed 自动放宽为 OR  —— 按"命中了几个词"排序（缺省搜索的核心）
    L3 fuzzy   trigram/编辑距离容错 + 同义词扩展 + 拼写建议

任一级有结果就停，并在返回里标注用到了哪一级，界面据此提示用户。
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

# bm25 列权重：ids > name > brand > dims > blob
W = (12.0, 8.0, 5.0, 2.0, 1.0)

LEVEL_LABELS = {
    "exact": "编码精确命中",
    "and": "全文匹配（所有关键词）",
    "prefix": "前缀匹配（没打完也算）",
    "relaxed": "已放宽（部分关键词）",
    "filter": "按属性筛选",
    "fuzzy": "模糊匹配（可能拼错）",
    "none": "没有命中",
    "browse": "浏览全部",
}

_PHRASE = re.compile(r'"([^"]+)"')
_ASCII = re.compile(r"^[A-Za-z0-9.\-_/]+$")


@dataclass
class Query:
    raw: str
    terms: list[str] = field(default_factory=list)     # 普通词
    phrases: list[str] = field(default_factory=list)   # "引号短语"
    excludes: list[str] = field(default_factory=list)  # -排除词
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
    """FTS5 词项转义：双引号包裹，内部双引号翻倍。"""
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
# 主检索
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

    # ---------------- F：只有属性过滤、没有关键词（thickness:18 colour:black） ------- #
    if not q.terms and not q.phrases and q.filters:
        where, ps = cat_clause()
        rows = con.execute(
            f"SELECT m.*, 0 AS score FROM material m WHERE {where} "
            f"ORDER BY m.ref_count DESC, m.name LIMIT ?", [*ps, 2000]
        ).fetchall()
        results = hydrate(rows)
        level = "filter"

    # ---------------- L0：编码精确命中 ---------------- #
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

    # ---------------- L1：加权全文 AND ---------------- #
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

    # ---------------- L1.5：前缀放宽（懒得打完） ---------------- #
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

    # ---------------- L2：逐级丢词（保精度优先：先丢掉没命中的词再 AND） ---------------- #
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

    # ---------------- L2.5：放宽为 OR（扩召回兜底） ---------------- #
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

    # ---------------- L3：模糊容错（词级相似度，不是整串比） ---------------- #
    if not results and (q.terms or q.phrases):
        probe = (q.terms + q.phrases)[0].lower()
        where, ps = cat_clause()
        # (a) 子串：兜住中文（FTS5 unicode61 不切中文词）
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
            # (b) 词级编辑距离：拿 probe 跟每个候选的「词袋」比，而不是跟整句比
            #     "luminux" vs 整名 "16mm White Melamine MDF" 相似度极低，
            #     vs 单词 "Laminex" 才有意义
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

    # ---------------- 空查询：浏览；有查询但全落空：明确报"没有命中" ---------------- #
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

    # ---------------- 属性过滤（thickness:18 / colour:black） ---------------- #
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
        # 注意：用了属性过滤就按过滤结果走，哪怕为空——否则会把未过滤的列表误当成结果
        results = kept

    # ---------------- 统一反序列化（模糊分支走的是纯 dict） ---------------- #
    for r in results:
        for k in ("attributes", "synonyms", "relations", "prices"):
            if isinstance(r.get(k), str):
                r[k] = json.loads(r[k] or ("{}" if k == "attributes" else "[]"))
        if isinstance(r.get("tags"), str):
            r["tags"] = [t for t in r["tags"].split(",") if t]

    # ---------------- 排序加权：相关性 + 常用度 - 归档惩罚 ---------------- #
    maxref = max([r["ref_count"] for r in results] or [1]) or 1
    maxscore = max([r.get("score") or 0 for r in results] or [1]) or 1
    for r in results:
        # 模糊分支的 score 本身就是 0~1 的相似度；FTS 分支是 bm25，做批内归一化
        rel = (r.get("score") or 0) if level == "fuzzy" else (r.get("score") or 0) / maxscore
        rel = min(1.0, rel)
        pop = (r["ref_count"] or 0) / maxref
        pen = 0.35 if r["status"] == "archived" else 0.0
        r["final_score"] = round(max(0.0, 0.70 * rel + 0.30 * pop - pen), 4)
    results.sort(key=lambda r: -r["final_score"])

    # ---------------- Facet ---------------- #
    facets = {}
    for key in ("category", "brand", "default_supplier", "unit"):
        facets[key] = {
            k: v for k, v in con.execute(
                f"SELECT {key}, COUNT(*) FROM material WHERE {key} IS NOT NULL "
                f"GROUP BY {key} ORDER BY COUNT(*) DESC LIMIT 12"
            ).fetchall() if k
        }

    # ---------------- 拼写建议（用词袋，而不是拿整句比） ---------------- #
    suggestion = None
    if level in ("fuzzy", "none") and q.raw:
        freq: dict[str, int] = {}
        for row in con.execute("SELECT * FROM material").fetchall():
            for t in set(_wordbag(row)):
                freq[t] = freq.get(t, 0) + 1
        cands = difflib.get_close_matches(q.raw.lower(), list(freq), n=6, cutoff=0.60)
        cands = [c for c in cands if c != q.raw.lower()]
        # 优先推荐"库里材料多的那个词"，避免把拼写纠正到一条冷门记录上
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
    """一条材料的可比对词袋：名称/品牌/类型/分类/同义词各自拆词。"""
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
    for term in (sys.argv[1:] or ["blum hinge", "blu", "16mm black", "luminux", "mdf", "台面"]):
        r = search(term)
        print(f"\n=== {term!r}  →  {r['level']} ({r['level_label']})  {r['total']} 条")
        for x in r["results"][:5]:
            print(f"   {x['final_score']:.3f}  [{x['category']:10}] {x['name'][:52]:54} ${x['unit_cost']}")
        if r["suggestion"]:
            print(f"   你是不是要找: {r['suggestion']}")
