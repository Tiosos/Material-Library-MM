# Material Library — universal material library · Design v1

> Positioning: **material master data (MDM) that is independent of any business system**. It can be used on its own as a query site, and can also be referenced and imported by joineryflow or any other system via API / file / SDK.
> Reference source: `github.com/Tiosos/joineryflow` (FastAPI + Postgres + Next.js; the Catalog module has 9 material tables and 14 product types).
> Version: v1 (to be iterated on feedback)

---

## 0. TL;DR

| Item | Conclusion |
|---|---|
| Data model | **One core table + type profiles + attribute KV**, replacing joineryflow's 9 physical tables (while keeping 100% of fields and bidirectional mapping) |
| Primary key | `material_uid` (ULID, `mat_xxx`), permanently stable across systems; no more composite `(material_type, id)` references |
| Search | **Four-level graceful degradation retrieval**: exact/prefix → weighted full text (AND) → automatic relaxation (OR) → trigram spelling tolerance + spelling suggestions |
| Delivery form | ① Postgres service + REST API ② single-file SQLite database (zero dependencies, copy it anywhere) ③ JSONL/CSV export ④ query site |
| Integration | OpenAPI + JSON Schema + Python/TS SDK + MCP server (so AI can query directly) |
| First milestone | **Phase 1 (2 weeks)**: import joineryflow's 9 tables + one search box that actually works |

This design ships with a **runnable prototype** (`proto/`) that you can search directly, to validate how "forgiving search" feels.

---

## 1. Understanding the requirement: what "keywords can be omitted when searching" means

This is the core requirement of the design. I break it into **four levels of omission**, and the design must support all four:

| Level | Scenario | Example | Mechanism |
|---|---|---|---|
| **L1 input omission** | Can't be bothered to type it all | `blu` → Blum hardware | Prefix matching + completion |
| **L2 hit omission** | You remember only a few of the words; not all of them need to match | typing only `blum hinge` for `blum soft close hinge` | AND first; drop to OR automatically when there are no results; sort by number of hits |
| **L3 dimension omission** | You don't know which field to search | typing `18mm black` finds the 18mm black board | one box searches all fields (SKU/name/brand/supplier/spec/colour/synonyms) |
| **L4 spelling omission** | Typos, colloquial names, mixed Chinese and English | `luminux`/`melamine`/`MDF`/`MDF fibreboard` | trigram similarity + synonym table + tokenisation |

Plus one experience constraint: **never show a blank screen on zero results**; instead offer "did you mean X" and the approximate results after relaxing the criteria.

---

## 2. Current state: what joineryflow already has

Read from the repository (not guessed):

**Assets (directly reusable)**
- 9 material tables: `board / edging / glass / timber / benchtop / hardware / appliance / custom_made / equipment_hire`, driven uniformly by `catalog/queries.py::REGISTRY`, with field definitions centralised in `*_FIELDS_TUPLE`
- 14 product type tabs (6 of which reuse a table and have no table of their own)
- Common columns: `sku / description / synonyms / default_supplier(_id) / default_lead_time_days / unit_cost / product_type / archived_at,archived_by / workspace_id`
- Type-specific fields: board's `colour/substrate/finish/sided/grain/rating/hmr/dimensions`, glass's `glass_type/edge_finish/power_point`, edging's `edge_type/roll_length_m/glued`, etc.
- Unit cost fallback rule: when `unit_cost` is empty, use each table's own cost column (`cost_per_sheet`/`cost`/`cost_per_slab`/`cost_per_unit`)
- Purchase history endpoint: `GET /catalog/{slug}/{mid}/purchase-history` — a natural **price history data source**
- Soft archiving, workspace isolation, image blobs, CV supplier code mapping (`cv_material_mapping`)

**Pain points (what the design must fix)**

| # | Problem | Impact |
|---|---|---|
| P1 | Search is `description ILIKE '%q%' OR sku ILIKE '%q%' OR product_type ILIKE '%q%'`, a single keyword | Multi-word, misspelled and colloquial queries all find nothing |
| P2 | No ordering, no relevance | Results follow insertion order; frequently used materials never rise to the top |
| P3 | Cross-type search needs tab switching; the API layer loops `for type_ in REGISTRY` nine times | Searching for "18mm black" you don't know which tab it is on |
| P4 | Primary keys are table-local auto-increments; cross-table references rely on the `(material_type, id)` composite | No stable ID can be handed to external systems; it breaks as soon as it is imported |
| P5 | No source provenance, no versions, no price history, no standard export format | Can't be trusted as "master data" externally; the numbers don't reconcile |

---

## 3. Architecture overview

```
                    ┌──────────────────────────────────────────────┐
   joineryflow ────▶│   ingest / ETL (mapping spec)                │
   FileMaker  ─────▶│   full + incremental (since=)                │
   CSV / manual ───▶│   conflicts logged, no silent overwrite      │
                    └──────────────┬───────────────────────────────┘
                                   ▼
                    ┌──────────────────────────────────────────────┐
                    │  Material Library (core)                     │
                    │  material + identifier                       │
                    │  + price + relation                          │
                    │  + profile (type profile)                    │
                    └──────────────┬───────────────────────────────┘
                                   ▼
     ┌─────────────────┬───────────┴────────┬──────────────────┐
     ▼                 ▼                    ▼                  ▼
 Query site (Web)      REST / GraphQL       Export (JSONL/     MCP server
 forgiving search      API + SDK            CSV/Parquet)       (direct AI query)
     │                 │                    │                  │
     └─────────────────┴────────────────────┴──────────────────┘
                                   ▼
              write-back → joineryflow PATCH /catalog/{slug}/{mid}
```

**Key decision: Material Library is an independent service; it is not stuffed into joineryflow.**
Rationale: joineryflow's materials are "business data scoped to a workspace", whereas master data must be reused across workspaces and across systems. The two are linked by `material_uid`, not merged.

---

## 4. Data model: Universal Material Record (UMR)

See [`schema/material.sql`](../schema/material.sql) for the full DDL and [`schema/material.schema.json`](../schema/material.schema.json) for the interchange format.

### 4.1 Five tables

| Table | Purpose | Problem solved |
|---|---|---|
| `material` | Core record (category, name, brand, unit, unit cost, attributes, synonyms…) | P3: one table unifies the 9 categories |
| `material_identifier` | Identifier book: `sku / ean / supplier_code / legacy_id / cv_code / slab_id / model_no` | P4: any system's code can be traced back |
| `material_price` | Price history (supplier, validity period, tiered prices) | P5: unit cost is no longer a bare number |
| `material_relation` | Substitute material / accessory / upgrade replacement | Offer substitutes immediately when something is unavailable |
| `material_profile` | Type profile (a JSON Schema defining which fields are specific to that type) | Adding a 10th material type needs no table change |

### 4.2 Key design: core + attributes, rather than 9 tables

```jsonc
{
  "material_uid": "mat_01J8ZQ8K3M5N7P9R2T4V6X8Y0A",
  "category": "board",
  "product_type": "Board",
  "name": "16mm Black Melamine",
  "brand": "Laminex Australia",
  "unit": "sheet",
  "unit_cost": 40.00,
  "currency": "AUD",
  "cost_basis": "ex_gst",
  "lead_time_days": 5,
  "default_supplier": "Laminex Australia",
  "synonyms": ["16-BLACK", "black melamine board", "melamine", "MDF black"],
  "tags": ["common", "carcass"],
  "attributes": {
    "thickness_mm": 16, "colour": "Black", "substrate": "MDF",
    "sheet_len_mm": 3600, "sheet_wid_mm": 1200, "finish": "Melamine", "sided": "Double"
  },
  "identifiers": [
    { "kind": "sku",       "value": "16-BLACK", "source_system": "joineryflow", "is_primary": true },
    { "kind": "legacy_id", "value": "BM-201",   "source_system": "joineryflow" }
  ],
  "source_system": "joineryflow",
  "source_id": "42",
  "source_payload": { "...": "raw row snapshot, used for reconciliation and rollback" }
}
```

**Why not just keep the 9 tables?** Because a "universal material library" keeps gaining types (consumables, paint, hardware fittings…), and creating a table for each new one is not sustainable. Core table + profile validation = flexible without losing control. The mapping layer guarantees that every row and every field of joineryflow can round-trip 1:1.

### 4.3 Mapping to joineryflow

See [`mapping/joineryflow.yaml`](../mapping/joineryflow.yaml) for the full specification. Key points:

- 9 tables → nine `category` values; type-specific fields go into `attributes` as-is (**key names unchanged**, to make write-back easy)
- `sku/code/internal_ref/slab_id/model_number/contract_ref` are all registered in `identifier`
- `unit_cost` follows the upstream fallback rule (`COALESCE(unit_cost, own cost column)`)
- `archived_at` → `status=archived`, never hard-deleted
- Write-back goes through `PATCH /catalog/{slug}/{mid}`; writing back archive status is **forbidden**, and changing prices already snapshotted on a quote is **forbidden**

---

## 5. Search design (the focus of this design)

### 5.1 Retrieval pipeline: four-level graceful degradation

The user types one term; the levels are tried in order and it stops as soon as there are results; results are labelled "these are the relaxed results":

```
q ──▶ L0 exact/prefix hit (exact identifier = code; name prefix match)
      │  no results ↓
      ├──▶ L1 weighted full-text AND (every word must appear)           ← default path
      │      no results / <3 rows ↓
      ├──▶ L2 relaxed to OR (rank by hit word count + field weight)     ← core of "forgiving search"
      │      no results ↓
      └──▶ L3 trigram fuzzy + synonym expansion + spelling suggestions  ← typos/colloquial names
```

This way "typing too many words" doesn't lose the result, and "typing the wrong characters" doesn't give a blank screen.

### 5.2 Field weights (solving L3 dimension omission)

Generated column `search_tsv`, with four weight levels:

| Weight | Field | Rationale |
|---|---|---|
| **A** | all identifiers (sku/code/ean/supplier code/legacy id) | entering a code is the most precise intent |
| **B** | `name` `brand` `model_no` | name/brand |
| **C** | `default_supplier` `product_type` `category` `unit` | classification dimensions |
| **D** | `description` `attributes` `synonyms` `tags` | catch-all, high volume |

```sql
-- L1: weighted full-text AND with relevance ranking
SELECT material_uid, name, category, unit_cost,
       ts_rank_cd('{0.1,0.2,0.4,1.0}'::float4[], search_tsv, query) AS score
FROM material, websearch_to_tsquery('simple', :q) query
WHERE search_tsv @@ query AND status <> 'archived'
ORDER BY score DESC, ref_count DESC
LIMIT 50;
```

### 5.3 Automatic relaxation (solving L2)

```sql
-- L2: first work out whether each word hits, then sort by "hit word count DESC, weight score DESC"
WITH q AS (SELECT unnest(string_to_array(:q, ' ')) AS term)
SELECT m.*,
       COUNT(DISTINCT q.term) FILTER (WHERE m.search_tsv @@ plainto_tsquery('simple', q.term)) AS hits
FROM material m CROSS JOIN q
WHERE m.status <> 'archived'
  AND EXISTS (SELECT 1 FROM q WHERE m.search_tsv @@ plainto_tsquery('simple', q.term))
GROUP BY m.material_uid
ORDER BY hits DESC, ts_rank_cd(m.search_tsv, plainto_tsquery('simple', :q)) DESC
LIMIT 50;
```

### 5.4 Fuzzy matching and spelling suggestions (solving L4)

```sql
-- L3: trigram similarity; also produce the "did you mean"
SELECT material_uid, name, similarity(name, :q) AS sim
FROM material
WHERE name % :q OR description % :q
   OR EXISTS (SELECT 1 FROM unnest(synonyms) s WHERE s % :q)
ORDER BY sim DESC LIMIT 20;
```

- `pg_trgm` GIN indexes back `%` similarity and prefix acceleration
- The **synonym table** accumulates automatically from three sources: the upstream `synonyms` column, corrections clicked on in past searches, and a manually maintained alias table
- Mixed Chinese/English: the `simple` dictionary does not tokenise Chinese, so Chinese goes through `pg_trgm` bigrams; if Chinese tokenisation is needed, add `pg_jieba`/`zhparser` (recommend re-evaluating in Phase 3)

### 5.5 Ranking (solving P2's lack of relevance)

```
final_score = 0.55 * text_relevance
            + 0.20 * popularity(ref_count, search_hits normalised)
            + 0.15 * exactness(exact code hit = 1, otherwise 0)
            + 0.10 * freshness(recently used/recently updated)
            − penalty(archived heavily down-weighted but not hidden)
```

### 5.6 Query syntax (for advanced users, but not mandatory)

| Syntax | Meaning |
|---|---|
| `blum hinge` | default: AND, OR after relaxation |
| `blum -hinge` | exclude |
| `"soft close"` | exact phrase |
| `thickness:18 colour:black` | filter by attribute (attribute names come from the profile) |
| `supplier:laminex 18mm` | mixed |
| empty query | return "recently used / recently updated" instead of blank |

### 5.7 Facet filtering

Always present on the right: category (9), brand, supplier, unit, price range, archived toggle; plus attribute filters that appear dynamically by type (e.g. thickness / colour for board, glass_type for glass). **Filters stack on top of search; they are not an either/or choice.**

---

## 6. How other systems reference / import it

| Method | Suits | Notes |
|---|---|---|
| **REST API** | Online systems | `GET /v1/materials?q=&category=&limit=&cursor=`, `GET /v1/materials/{uid}`, `POST /v1/materials/bulk` |
| **Incremental sync** | Keeping two systems consistent | `GET /v1/materials?since=<updated_at>`; external systems store only `material_uid` |
| **File import** | One-off / offline | `materials.jsonl` (one record per line, streaming), `materials.csv` (attributes flattened into `attr_*` columns), Parquet |
| **Contract** | Generating clients | OpenAPI 3.1 + JSON Schema; Python/TS/C# clients can be generated automatically |
| **SDK** | Developer experience | `pip install materiallib` / `npm i @materiallib/client`, one line `search("blum hinge")` |
| **MCP server** | AI/LLM invocation | Exposes the Material Library as tools, so Copilot/Claude can ask "how much is an 18mm black board" directly |
| **Single-file SQLite** | Embedding / portable | The whole library packaged as one `.sqlite`; anyone you copy it to can query it, zero dependencies |

**Consumer-side conventions (written into the contract, important)**
1. External systems **store only `material_uid`**; do not store local auto-increment IDs
2. When snapshotting a price, **store `valid_from` and `source_system` along with it**; don't copy just the number
3. When a material is archived, external systems **must not crash**; treat it as "historically valid"

---

## 7. Query site UI (usable on its own)

- **One large search box at the top**, focus shortcut `/`, search as you type (debounce 150ms)
- **Category tree on the left** (9 categories + frequent/favourites), **result cards on the right** (image + name + SKU + supplier + unit cost + key attribute tags)
- **Top of the results area**: hit count, which retrieval level was used ("exact / relaxed / fuzzy") + a one-click "exact only"
- **Zero-result page**: spelling suggestions + relaxed results + "N results for XX"
- **Detail page**: all attributes, price history chart, substitutes, purchase history, original source, copy UID / export JSON
- **Bulk actions**: select → export JSONL/CSV; generate a share link
- Tech: reuse joineryflow's existing Next.js style (Inter + JetBrains Mono, design tokens), or make it a minimal static site

---

## 8. Roadmap

| Phase | Goal | Output | Duration |
|---|---|---|---|
| **P0** | Data dictionary finalised | `material.sql`, `material.schema.json`, `joineryflow.yaml` (**done, pending review**) | 3 days |
| **P1** | Usable | joineryflow 9-table ETL + REST queries + basic search (L0/L1) + sample data import | 2 weeks |
| **P2** | Good to use | four-level degraded search (L2/L3), facets, ranking, zero-result suggestions, query site UI | 2 weeks |
| **P3** | General-purpose | all export formats, SDK, incremental sync, write-back, price history, substitutes | 2 weeks |
| **P4** | Intelligent | MCP server, vector semantic search, image search, automated supplier price scraping | Depends on feedback |

---

## 9. Recommended stack and alternatives

| Dimension | **Recommendation** | Alternative | Trade-off rationale |
|---|---|---|---|
| Storage/retrieval | **PostgreSQL** (`tsvector` + `pg_trgm`) | Meilisearch / Typesense / ES | PG is the same stack as joineryflow, so no new ops burden; below 500k rows PG is enough. **If "out-of-the-box typo tolerance + Chinese tokenisation + millisecond latency" is required, switching to Meilisearch is less work** (it supports typotolerance natively, and this design's L3 can be handed to it wholesale) |
| Standalone/embedded | **SQLite + FTS5** | DuckDB | For prototypes and "copy it and it works" scenarios |
| API | **FastAPI** | — | Consistent with joineryflow, OpenAPI comes for free |
| Front end | Reuse joineryflow's Next.js | Static HTML | Depends on whether it should merge into the existing system |
| Primary key | **ULID** | UUID v7 | ULID lexicographic order ≈ time order: index-friendly and readable |
| Semantic retrieval | `pgvector` (later) | External vector store | Only adopt it if "intent search" is genuinely needed |

> **One important recommendation**: don't reach for a search engine yet. Finish P1–P2 with PG full text + trigram, and measure the complaint rate for "can't find it" on real data; if it is still high, bring in Meilisearch then — by that point the data pipeline is ready and switching costs are low.

---

## 10. Decisions I need from you (these determine how v2 is written)

1. **Is the data scope just joineryflow's 9 categories, or should consumables/paint/packaging etc. be included too?** (affects the profile design)
2. **Master data ownership**: is Material Library the single source of truth (joineryflow becomes a consumer), or is it two-way sync? I lean towards the former, but write-back would be limited.
3. **How to handle workspaces**: is master data shared across workspaces (a board is globally unique), or one copy per workspace? I lean towards **global sharing + per-workspace price/supplier overrides**.
4. **Deployment form**: standalone service / single-file SQLite / both?
5. **Chinese support**: material names are mostly English today; do we need Chinese names and Chinese search?
6. **Should prices go into the library**: beyond carrying `unit_cost`, should purchase history be carried in to build price curves? (I lean towards yes, but need confirmation that there are no commercially sensitive issues)

---

## Appendix: the runnable prototype shipped with this design

`proto/` holds a zero-dependency prototype for validating the search design in Chapter 5:

```bash
cd proto
python3 build_db.py      # generate materials.sqlite (sample data + FTS5 index)
python3 serve.py         # http://localhost:8848
```

- The sample data is built to joineryflow's real field structure (9 categories, real brands: Laminex / Blum / Caesarstone / Miele…)
- It implements the full degradation chain of L0 prefix → L1 AND → L2 relaxed OR → L3 fuzzy/synonyms
- The UI tells you "which retrieval level was used this time", so you can judge directly whether it feels right

**I suggest looking at the prototype first, then coming back with "which search disappointed you" — that is far more effective than discussing the document.**
