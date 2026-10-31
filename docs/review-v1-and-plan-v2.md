# Design v1 Review · Peer Project Comparison · v2 Revision Plan

> Status note: This document is a review record of **v1**. Items such as §2.1 (DDL cannot execute), §2.2 (filtering after LIMIT), and §2.3 (level label misjudgment) have been fixed in **v2**; see [`data-model-v2.md`](data-model-v2.md) for implementation status.

> Review targets: this repository's `docs/design-v1.md`, `schema/material.sql`, `schema/material.schema.json`, `mapping/joineryflow.yaml`, `proto/*`
> Method: read the code + run the prototype + compare against public standards and peer products
> Conclusion date: 2026-10-29

---

## 0. One-Sentence Conclusion

**The direction is right and the model is basically usable, but this version cannot yet ship as "master data."** Three levels of problems:

1. **Only the prototype runs; the DDL does not** — the `search_tsv` generated column in `material.sql` uses a subquery, and PostgreSQL refuses to create the table (see §2.1). In other words, the Postgres version promised in the document does not currently exist.
2. **The "forgiving search" implementation does not match the document and has 3 bugs that cause users to see wrong results** (filters applied after LIMIT, level label misjudgment, words dropped from the tail).
3. **The model lacks 4 things needed for master data to hold up**: a classification hierarchy, units/dimensions, variants (product families), and change events. The longer these four are left unaddressed, the more expensive they become to fix.

Among peer projects, **none** offers a directly usable "joinery material master database" — the slot is empty. But PIM (UnoPim/Pimcore/Akeneo), construction product data standards (ETIM/BMEcat, ISO 23387, IFC), and EPD data (openEPD/ILCD, EC3) have already built 80% of the mechanics: **copy the mechanics, not the products.**

---

## 1. Peer Project Map: Who Is Solving the Same Problem

Ordered by how directly they compete for the same ground:

### 1.1 Engineering Material Databases (properties type) — Closest, but a Different Lane

| Project | What it is | Worth borrowing | No need to copy |
|---|---|---|---|
| [MatWeb](https://www.matweb.com) | General engineering material property database (tens of thousands of materials; tensile/density/temperature resistance) | Property + test standard + source as a triple; same-name materials side by side from multiple sources | Read-only reference library; no coding/pricing/procurement |
| Total Materia | Metal/plastic grades ↔ equivalent grades (cross-standard comparison) | **An equivalence table for "one material = multiple standard grades"**, exactly what `material_relation(substitute)` should grow into | Heavy-industry grade system; not useful for joinery |
| Granta MI / Material Intelligence | Enterprise material data management; properties carry conditions, sources, and versions | **Property values are not scalars**: a six-tuple of `(value, unit, test method, condition, source, validity period)`; data governance and approval workflows | Commercial licensing, heavily GIS-oriented, too costly |
| [open-material-db](https://github.com/floating-gates/open-material-db) | Minimalist SQLite material library script | Confirms that "single-file SQLite distribution" is viable | No search, no coding system |

**What this means for you**: Putting properties into `attributes jsonb` is right for now (it is fast), but when you need to answer "does this board meet spec at 18mm," `{"thickness_mm": 18}` has no source, no tolerance, and no standard, so it will not convince anyone. I recommend a **dual track**: JSONB for the fast path, plus a separate `material_measure` for "measures with provenance," used only for fields that carry accountability, such as thickness/density/fire rating/VOC/carbon emissions.

### 1.2 Open-Source PIM / MDM — Most Complete Mechanics, Design Can Be Copied Directly

| Project | What it is | Worth borrowing |
|---|---|---|
| [UnoPim](https://github.com/unopim/unopim) (Laravel, MIT) | Open-source PIM: attribute families, product families, multi-channel, 33 languages, version control, completeness scoring, **DPP templates**, webhooks, optional Elasticsearch | ① **Measurement Families**: define units and conversions once, then capture properties as "value + unit"; ② **Product Completeness**: score against required fields per family; ③ **Version control** + publishing channels; ④ variant axes (multi-level) |
| [Pimcore](https://pimcore.com) (PHP, GPL) | All-in-one PIM/MDM/DAM: data objects + classification store + workflows | Classification store = a mature version of your `attributes` + `profile`; a data quality rules engine |
| [Akeneo](https://www.akeneo.com) (PHP, community edition) | Veteran open-source PIM built around the family/attribute/channel trio | Completeness and localization models; import/export profiles |
| Odoo / ERPNext Item module | Material master data from an ERP perspective | Material coding rules, unit-of-measure conversion, BOM links — **your `material_relation(bundled_with)` can be treated as a lightweight BOM** |

**What this means for you**: v1's "core + profile + KV" is essentially the PIM data object model; the direction is right. What is missing are four things PIM has already proven: **units, completeness, versions, variants**. Copy UnoPim's semantics for these four directly; do not copy its UI or PHP stack.

### 1.3 Construction Product Data Standards — Determines Whether You Can Exchange with the Outside World

| Standard | Role | How to use it |
|---|---|---|
| **ETIM** + **BMEcat** → **ETIM xChange 2.0** | Category feature model and catalog exchange format for the European building materials/hardware wholesale industry; ETIM is replacing BMEcat XML with the JSON-based xChange ([ETIM white paper, 2026-02](https://etim.de/wp-content/uploads/2026/03/Whitepaper-vom-BMEcat-zu-ETIM-xChange2.0_18.02.2026.pdf)) | Your hand-written `thickness_mm`/`colour` keys already exist in ETIM with standard feature codes, data types, enumeration values, and units. **Even mapping just 20 features would let supplier catalogs go straight into the database** |
| **ISO 23387:2025** (BIM data templates) | Defines the structure of "construction object data templates": features + units + constraints ([ISO 23387:2025](https://online.standard.no/en/ns-en-iso-23387-2025)) | Your `material_profile.field_schema` is a private version of a data template; once aligned, it can be consumed directly by BIM tools |
| **IFC 4.3 / bSDD** (ISO 16739) | Property sets such as `IfcMaterial` and `Pset_MaterialCommon` in BIM ([buildingSMART Pset definitions](https://standards.buildingsmart.org/MVD/RELEASE/IFC4/ADD2_TC1/RV1_2/HTML/schema/ifcmaterialresource/pset/pset_materialwoodbasedbeam.htm)) | If boards/hardware are to enter design institutes or cabinet design software, property set naming should lean toward Pset |
| **eCl@ss / UNSPSC** | Cross-industry classification and feature codes | The category tree should also carry 1–2 external classification codes; do not rely only on your own `category` |
| **GS1 / GTIN** | Product barcodes | `material_identifier(kind='ean')` already exists; add GTIN-13/14 check digits and GS1 Digital Link resolution |

**Key judgment**: ETIM xChange is the most "actionable" standard for you in the 2026 wave — it is small, JSON-based, covers hardware/boards/glass exactly, and has a ready-made feature dictionary. **I recommend using `ETIM class + feature` as the v2 skeleton for `material_profile`** instead of continuing to invent key names.

### 1.4 Sustainability / EPD / Digital Product Passport — Will Go from "Bonus" to "Entry Requirement" Within Two Years

| Object | Current state | How to use it |
|---|---|---|
| **openEPD** (JSON/JSON-LD) | EPD exchange format led by Building Transparency, used in the EC3 ecosystem | Preferred external format for a future `material_epd` table |
| **ILCD+EPD** (XML) | Common format for European national EPD databases (Ökobaudat, INIES, IBU) | Compatible on the import side |
| **EC3 API** | Free REST API with about 200,000 digitized EPDs; requires an account and quota ([docs](https://docs.buildingtransparency.org/ec3/api-and-integrations)) | Can match by brand/product name and backfill GWP, creating a value-added "material → carbon emissions" field |
| **ISO 22057** | Data template for EPD data in BIM ([EPD Guide](https://epd.guide/standards-and-schemas/digital-epds-from-pdfs-to-plug-and-play-data-in-xml-or-json)) | Same as above: align property sets |
| **ESPR / CPR 2024/3110 → DPP** | EU Digital Product Passport; construction materials will be phased in from 2026 ([industry analysis](https://myproductpassport.eu/fr/sectors/construction)) | Not mandatory in the Australian market in the short term, but "material-level carbon emissions + recycled content + disassemblability" is already bid language |

**What this means for you**: You do not need to build DPP now, but **reserve the field space now** (a `material_epd` table or an `epd.*` namespace in `attributes`), or you will have to touch the core table later. This is a classic case of "not doing it when it is cheap, unable to afford it when it is expensive."

### 1.5 Material Passports / Circular Economy

| Project | What it is | What to borrow |
|---|---|---|
| [Madaster](https://madaster.com) | Building-level material passport platform (component → material → weight → residual value → disassembly) | Multi-level attribution of "material → component → building"; circularity attributes (recycled content, recycled material, disassembly level) |
| Circularise and others | Supply chain traceability (batch, origin, compliance declarations) | Batch-level traceability: your `source_payload` is only a single-row snapshot and lacks a "batch/arrival" layer |

### 1.6 Search and Discovery — What You Are Hand-Building Is Already Commercialized Elsewhere

| Option | What it gives you | When to switch |
|---|---|---|
| PostgreSQL `tsvector` + `pg_trgm` | Your current design | Data < 100,000 rows and you can accept "Chinese handled by trigram fallback" |
| [Meilisearch](https://www.meilisearch.com/comparisons/meilisearch-vs-typesense) / [Typesense](https://typesense.org/typesense-vs-meilisearch/) | Fuzzy matching, instant prefix search, facet counts, hybrid vector search, out of the box | When mixed Chinese search + millisecond autocomplete become hard requirements |
| SQLite FTS5 `trigram` tokenizer | The portable version can also do substring/fuzzy search without a full-table `difflib` | Usable immediately to fix the prototype |
| Vector + RRF hybrid | Intent queries such as "find hinges that can replace Blum" | After you have ≥500 real query logs and the keyword approach fails evaluation |

### 1.7 Joinery/Cabinetmaking Vertical — The Real Gap

Cabinet design software (Cabinet Vision, Mozaik, 2020 Design) all have "catalog/price list" modules, and hardware and board manufacturers distribute catalog data via BMEcat/ETIM or their own portals, but there is no **open, queryable material master data that any system can reference**.

**This is your positioning**: not "building another PIM," but "**a material semantic layer for the joinery industry + search that tolerates bad input**."
The moat is not the table schema (anyone can copy it) but these three things: **① a vocabulary of industry colloquial names/brands/specifications; ② the 1:1 mapping to joineryflow and real data; ③ the feel of forgiving search**.

---

## 2. Test Findings: Concrete Defects in the Current Version

`proto/` is working (100 sample records). Each item below includes a way to reproduce it.

### 2.1 Critical: DDL Cannot Execute (P0)

`schema/material.sql:184` defines `search_tsv` as a generated column whose expression contains a subquery against `material_identifier`:

```sql
ALTER TABLE material ADD COLUMN search_tsv tsvector
    GENERATED ALWAYS AS ( ... (SELECT string_agg(i.value,' ') FROM material_identifier i ...) ... ) STORED;
```

PostgreSQL explicitly forbids this: a generated expression **may only use immutable functions, must not contain subqueries, and must not reference anything outside the current row** ([PG 19 §5.4](https://www.postgresql.org/docs/19/ddl-generated-columns.html)). This `ALTER` will definitely fail, and running things as they stand will stop here — in other words, the Postgres version promised in the document **has never actually been run successfully** (the prototype uses a separate SQLite DDL; see `proto/build_db.py`).

**Fix**: make `search_tsv` a regular column maintained by triggers (function bodies may contain subqueries):

```sql
ALTER TABLE material ADD COLUMN search_tsv tsvector;

CREATE OR REPLACE FUNCTION material_build_tsv(p_uid text) RETURNS tsvector
LANGUAGE sql STABLE AS $$
  SELECT setweight(to_tsvector('simple', coalesce(string_agg(i.value,' '),'')), 'A')
      || setweight(to_tsvector('simple', coalesce(m.name,'')||' '||coalesce(m.brand,'')||' '||coalesce(m.model_no,'')), 'B')
      || setweight(to_tsvector('simple', coalesce(m.default_supplier,'')||' '||coalesce(m.product_type,'')||' '||coalesce(m.category,'')||' '||coalesce(m.unit,'')), 'C')
      || setweight(to_tsvector('simple', coalesce(m.description,'')||' '||coalesce(m.attributes::text,'')||' '||coalesce(array_to_string(m.synonyms,' '),'')||' '||coalesce(array_to_string(m.tags,' '),'')), 'D')
  FROM material m LEFT JOIN material_identifier i ON i.material_uid = m.material_uid
  WHERE m.material_uid = p_uid GROUP BY m.material_uid;
$$;

CREATE FUNCTION material_tsv_sync() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.search_tsv := material_build_tsv(NEW.material_uid);
  NEW.updated_at := now();
  RETURN NEW;
END $$;
-- BEFORE INSERT/UPDATE on material, plus triggers on material_identifier to write back to the parent row
```

**Two related issues**: ① There is no `updated_at` trigger anywhere in the database, so `?since=` incremental sync always gets the import time; ② the `search_text` column is declared but never assigned, making it a dead column.

### 2.2 Critical: Filters Execute After LIMIT (P0, produces empty results)

`proto/search.py:290` fetches 40 rows first, then filters properties in Python. Test results:

| Query | Actual return | Expected |
|---|---|---|
| `thickness:18` | `filter`, 5 rows | ✅ |
| `melamine thickness:18` (limit=40) | 3 rows | ✅ No problem exposed, by luck |
| `melamine thickness:18` (limit=5) | **2 rows** | True value is 3 — filtering happens only on the 5 rows fetched, silently dropping 1 |
| `blum thickness:18 hinge` | `and`, 0 rows | Result depends on "whether it is in the first 40 rows" |

**Fix**: push filter conditions down into SQL, using the same `WHERE` as search:

```sql
AND (attributes->>'thickness_mm')::numeric BETWEEN :v - 0.51 AND :v + 0.51
AND attributes @> '{"colour":"Black"}'::jsonb
```
The `GIN (attributes jsonb_path_ops)` index already exists; for numeric ranges, add an expression index on `((attributes->>'thickness_mm')::numeric)`.

### 2.3 Medium: Search Level Labels Are Unreliable (distorts your judgment of "feel")

Three-level test results:

| Query | Returned `level` | Problem |
|---|---|---|
| `b` | `exact` (27 rows) | A single letter is treated as an "exact identifier hit"; users lose trust at first glance |
| `blu` | `and` | It actually goes through the prefix branch (`search.py:198`) but is labeled `and`; the `prefix` entry in `LEVEL_LABELS` is never used |
| `450 blum hinge` | `relaxed`, 10 rows | Words are dropped from the **tail**, dropping `hinge` (the core term) |
| `blum hinge 450` | `relaxed`, 3 rows | Same rule, but it happens to drop `450` (the right result) — **right or wrong depends on word order** |

**Fix**:
1. `exact` only when "the entire query string equals the full value of an identifier," or "a single token that is an exact prefix of an identifier and at least 4 characters long";
2. The level is determined by the **stage that actually matched**; label the prefix branch `prefix`;
3. Dropping is decided by "whether the word matched" (as the document says); implement it as: test each word for a match → drop the ones that do not match → AND the remaining words; do not drop by position. This is equivalent to the `hits DESC` ordering in §5.3 of the document, but preserves precision first.

### 2.4 Medium: L3 Fuzzy Matching Is a Full-Table Scan + `difflib`

`search.py:259` reads the entire table into memory and computes bag-of-words similarity for every record. At 100 rows it does not matter; **at 10,000 rows it means millions of `SequenceMatcher` calls per request**, and at 50,000 rows it basically hangs.

**Fix** (without adding new dependencies):
- SQLite side: use the `FTS5 trigram` tokenizer (SQLite ≥ 3.34) for substring/fuzzy search, or `spellfix1` for spelling;
- PG side: `pg_trgm`'s `%` + `<%` (word similarity) + GIN; it is already in the schema, just unused by the code;
- Separate "candidate recall" from "candidate re-ranking": the DB recalls only the top 200, then Python does fine ranking.

### 2.5 Medium: Scores Are Normalized Within a Batch, So They Are Neither Comparable nor Cacheable

`search.py:319-327` divides by the batch maximum → the top result always scores 1.0, so you cannot set cross-query thresholds, run A/B tests, or cache rankings. Negating bm25 before normalizing is the right direction, but the semantics are lost.

**Fix**: keep the raw `-bm25` and apply a monotonic mapping `1/(1+exp(-x/8))`; add popularity, exactness, and freshness using the weights in §5.5 of the document, **with no in-batch normalization**.

### 2.6 Medium: Facets Do Not Narrow with the Query

The facets in `search.py:330` run `GROUP BY` over the whole table, independent of the current results; there are also no attribute facets (board thickness, colour), even though the first step for joinery users looking for material is exactly "18mm + black." The "filters combined with search" promised in §5.7 of the document is not implemented at the implementation layer.

**Fix**: reuse the same `WHERE` in facet queries (without pagination); generate attribute facets dynamically from the 3–5 highest-weight keys in `material_profile`'s `search_weight`; return `count`, not just names.

### 2.7 Model Layer: Four Structural Gaps

| # | Gap | Current state | Consequence |
|---|---|---|---|
| M1 | **Classification has only one level** | `category` is 11 flat values | "board > melamine > MDF > 18mm" cannot be expressed; cross-category statistics/merging all rely on hardcoding |
| M2 | **Properties have no units/dimensions** | `attributes` is free-form JSON, and `thickness:18` hardcodes `thickness_mm` (`search.py:295`) | `18` and `0.018 m` are not comparable; "18mm ±0.2" has nowhere to live; new properties require code changes |
| M3 | **No variants/product families** | One row = one SKU | The same décor in 16/18/25mm becomes three unrelated records; search is flooded by same-family results, and prices cannot be compared side by side |
| M4 | **No change events** | Only `updated_at` + `rev`, no history table | Write-back, webhooks, conflict tracing, and auditing are all impossible; `rev` is a deceptive field |

Two other "small holes" will bite with real data: `material_identifier.source_system` is nullable but part of the primary key (a PK is implicitly NOT NULL, so inserts without a source fail outright); and `UNIQUE (material_uid, kind, source_system)` forbids two codes from the same source for the same material (the same board in two sheet sizes as two SKUs is very common).

### 2.8 Engineering Debt (not fatal, but slows things down)

- `schema/material.schema.json` has no `schema_version` field, and `additionalProperties:false` means **adding a field is a breaking change**;
- The `material_profile` table exists, but the repository **has no profile definition files at all** (which fields board/glass have exists only in a documentation table) — validation is just a slogan;
- The prototype schema has drifted from `material.sql` (the prototype flattens `ref_count`/`prices`/`relations` into the `material` table and adds extra `dims`/`blob` columns);
- The FTS5 table is populated once and **has no triggers** (around `build_db.py:413`), so any UPDATE leaves the index stale;
- `_ascii_only` is dead code;
- JSONL exports have no `schema_version` or `exported_at`, so recipients cannot tell which version they have.

---

## 3. Six Capabilities to Add (ranked by cost-effectiveness)

1. **Units and dimensions (best value)** — write property values as `{value, ucum}` and convert uniformly when filtering. Introduce [UCUM](https://ucum.org) codes (`mm`, `m2`, `kg`, `L`) plus a `unit` table. **This makes the three inputs `thickness:18`, `thickness:1.8cm`, and `18mm` equivalent**, directly delivering on the "forgiving search" promise.
2. **Product families / variants** — `material.parent_uid` + `variant_axis`; collapse by family in search by default ("Laminex Black Melamine (12 sizes)"), expand on click. In joinery scenarios, this is the experience dividing line.
3. **Completeness scoring + duplicate detection** — compute `completeness` from the profile's required fields (UnoPim's approach); during import, use `pg_trgm` on `name+brand` to find duplicate candidates above 0.6 for manual merging. **Without this step, "master data" rots at 3% per week.**
4. **Change events / version table** — `material_event(seq, uid, type, payload)` as the single write outlet; sync, webhooks, and rollback all read from it. It is more reliable than `updated_at` (it survives clock skew, concurrent writes, and bulk imports).
5. **Search instrumentation + evaluation set** — log `(query, level, total, clicked_uid)`; maintain 30–50 "standard query → expected uid" cases in `eval/golden.yaml`, and run MRR@10 and zero-result rate in CI. **§9 of the document says "measure complaint rate with real data," but without instrumentation you can never measure it.**
6. **Export support for an ETIM xChange / BMEcat subset** — not full coverage; start with bidirectional mapping for 20 board/hardware features. This is the shortest path to turn "can be referenced by external systems" from a slogan into fact.

---

## 4. v2 Revision Plan

In three batches; **batch A does not change the architecture**, and can turn v1 from "on paper" into "usable" within a week.

### Batch A: Fix Correctness (1 week, no changes to the core model)

| # | Change | Acceptance |
|---|---|---|
| A1 | Maintain `search_tsv` with triggers; add an `updated_at` trigger; drop the dead `search_text` column | `psql -f schema/material.sql` runs with zero errors; `\d+ material` shows the tsv and index |
| A2 | `material_identifier`: `source_system NOT NULL DEFAULT 'unknown'`; keep the primary key `(kind,value,source_system)`; replace `UNIQUE(uid,kind,source_system)` with a regular index | A material can carry multiple same-source SKUs; codes without a source can be inserted |
| A3 | Push filters down (SQL layer); use range comparison for numeric properties | `melamine thickness:18` returns 3 rows at both limit=5 and limit=40; filters can only narrow, never lose results to pagination |
| A4 | Rewrite level determination (exact requires a whole-string hit; prefix is its own tier; dropping is based on match, not position) | `b` → `and/prefix`; `450 blum hinge` and `blum hinge 450` return the same result set |
| A5 | Switch scores to a monotonic mapping and remove in-batch normalization | The same document is comparable across queries; top1 is no longer always 1.0 |
| A6 | Facets narrow with the query + attribute facets + count; add keyset pagination `(score, uid)` | After searching `18mm black`, brand facets contain only the matching brands |
| A7 | Replace prototype L3 with FTS5 trigram (or `pg_trgm`); remove full-table `difflib` | P95 < 100ms at 10,000 rows |
| A8 | Add `schema_version` to the contract; include `exported_at` in exports; add 11 profile JSON files | `jsonschema` validates the samples; `profiles/` contains 11 files |
| A9 | Instrumentation tables `search_log` / `search_click` + `eval/golden.yaml` (30 cases) | Run `pytest eval` once to output MRR/zero-result rate |

### Batch B: Fill In the Model (2–3 weeks)

| # | Change | Description |
|---|---|---|
| B1 | `material_category`(code, parent, path, level) + external classification code table `material_classification(system, code)` | Keep the `material.category` text column for one version for compatibility |
| B2 | `party` (supplier/brand entities) + alias table; `material.brand_id` / `default_supplier_id` | Solve the three facets "Laminex / Laminex Australia / LAMINEX" |
| B3 | `unit`(ucum, dimension, si_factor) + `material_measure(uid, key, value_num, ucum, source, method, valid_from)` | JSONB remains the fast path; properties requiring accountability go into the measure table |
| B4 | `material_family` + `material.parent_uid` + `variant_axis` | Collapse by default in search; expand with `?expand=variants` |
| B5 | `material_revision` (snapshots) + `material_event` (outbox) | All writes go through events; consumers pull incrementally by `seq` |
| B6 | `material_name(uid, locale, name, is_primary)` + per-language search columns | Use `pg_bigm`/`zhparser` or trigram for Chinese; do not count on the `simple` dictionary |
| B7 | Completeness scoring + duplicate candidates + merge workflow (merge keeps a tombstone pointing to the golden record) | Merging is MDM's rite of passage; without it, three imports produce three copies of "16mm black" |
| B8 | MCP server (thin REST wrapper) + `llms.txt` | Move ahead of P4 in the v1 roadmap — one day of cost, and the benefit is "AI can query materials directly" |
| B9 | Import pipeline: BMEcat/ETIM files → UMR mapping (support boards + hardware first) | One catalog sent by a supplier can go straight into the database |

### Batch C: Ecosystem and Differentiation (on demand)

| # | Change | Trigger condition |
|---|---|---|
| C1 | ETIM xChange 2.0 / BMEcat export | When an external buyer asks for a catalog |
| C2 | `material_epd` (openEPD/ILCD references, GWP, recycled content) + DPP readiness | When bids require carbon emissions, or customers are in the EU |
| C3 | Hybrid search (BM25 + vector + RRF) | Evaluation MRR under the keyword approach is < 0.6, and there are 500+ query logs |
| C4 | Switch to Meilisearch/Typesense | Mixed Chinese search + millisecond autocomplete become hard requirements; **do not switch before batch A** |
| C5 | Batch/arrival traceability (supply chain traceability) | When real purchasing and arrival data exists |

### Things Not Recommended (saves time)

- **Do not build a full PIM back office** (workflows/permissions/DAM/multilingual UI is a six-month project); the UI should only be a "query site + light editing";
- **Do not create a physical table for each material type** (v1's judgment is right; stick with it);
- **Do not adopt a vector database now** (without an evaluation set, tuning is guesswork);
- **Do not build GraphQL** (REST + field selection parameters are enough, and OpenAPI is free);
- **Do not build your own Chinese tokenizer** (use `pg_bigm`/trigram, or hand it to Meilisearch);
- **Do not build bidirectional real-time sync** (in v2, one-way + explicit "write-back request" approval reduces conflicts by an order of magnitude).

---

## 5. Six Questions That Need Your Decision

Each question includes my default recommendation; you can reply only "go with the recommendation" or rewrite it.

| # | Question | Options | My recommendation |
|---|---|---|---|
| Q1 | **Positioning**: internal tool, or eventually external/sold to peers? | A internal / B external service | A first, but **design the data contract for B** (schema_version, ETIM mapping, reserved multi-tenant fields) |
| Q2 | **Source of truth direction**: is this library the single source of truth, or bidirectionally synced with joineryflow? | A one-way / B bidirectional | A. joineryflow only reads this library; write-back becomes "request-approval-event," with no real-time bidirectional sync |
| Q3 | **Variants**: should 16/18/25mm in the same décor be one product family or three independent records? | A family+variants / B independent | A. The later you change it, the more expensive it gets; this is core to batch B |
| Q4 | **Scale**: how many material records are expected within three years? (determines PG vs Meilisearch and how L3 is implemented) | <5k / ~50k / >500k | If <50k, PG is sufficient; only consider a search engine above 500k |
| Q5 | **Chinese/multilingual**: do you need Chinese names and Chinese search? Who maintains translations? | No / Yes, manual / Yes, AI-generated | Yes (mixing Chinese and English names is very common in the industry); start with AI generation + manual spot checks |
| Q6 | **Sustainability fields**: reserve space now, or add them when there are customers? | Reserve now / Later | Reserve now: an empty `material_epd` table + the `epd.*` namespace in `attributes`; the cost is almost zero |

One additional **non-technical** question (more critical than the six above): **where does the first batch of real data for this library come from?** A live export from joineryflow, the Hartwood seed, or catalogs supplied by suppliers? The data source determines the priority of B9 (import pipeline) and B7 (dedupe and merge) — if the first batch is supplier catalogs, B9 should be moved up and done immediately after batch A.

---

## 6. References

- PostgreSQL 19, [Generated Columns](https://www.postgresql.org/docs/19/ddl-generated-columns.html) — generated columns must not contain subqueries
- [ISO 23387:2025](https://online.standard.no/en/ns-en-iso-23387-2025) — BIM data templates
- [ETIM white paper: Vom BMEcat zu ETIM xChange 2.0 (2026-02)](https://etim.de/wp-content/uploads/2026/03/Whitepaper-vom-BMEcat-zu-ETIM-xChange2.0_18.02.2026.pdf)
- [buildingSMART IFC4 Pset definitions](https://standards.buildingsmart.org/MVD/RELEASE/IFC4/ADD2_TC1/RV1_2/HTML/schema/ifcmaterialresource/pset/pset_materialwoodbasedbeam.htm)
- [EC3 API documentation](https://docs.buildingtransparency.org/ec3/api-and-integrations) — about 200,000 digitized EPDs
- [Digital EPDs: ILCD+EPD / openEPD](https://epd.guide/standards-and-schemas/digital-epds-from-pdfs-to-plug-and-play-data-in-xml-or-json)
- [Construction Products DPP Requirements: 2026](https://myproductpassport.eu/fr/sectors/construction)
- [UnoPim](https://github.com/unopim/unopim) — open-source PIM: measurement families / completeness / version control / DPP
- [Meilisearch vs Typesense](https://www.meilisearch.com/comparisons/meilisearch-vs-typesense) · [Typesense comparison](https://typesense.org/typesense-vs-meilisearch/)
