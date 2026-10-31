# B9 Import Pipeline (Free-Form Supplier Headers)

> Goal: **do not change the supplier's sheet**; each supplier exports files with the headers it is used to, and we absorb them with a single mapping config.
> Deliverables: `ingest/` (read tables + mapping + validation + database writes), `mappings/*.yaml` (one per supplier), `scripts/import_file.py` (CLI), `ingest/pdf_blum.py` (catalogue PDF extraction).

---

## 1. Two Entry Paths

```
CSV / XLSX ──┐
             ├─→ header mapping (mappings/*.yaml) ─→ validation ─→ DB writes (material/family/measure/identifier/price)
PDF catalogue┘        ↑
   ↑                  └─ the extractor first turns the PDF into "one table" (each supplier has its own layout, so one version per layout)
```

- **Spreadsheets** are the main path: the Excel / CSV a supplier provides goes straight in.
- **PDF catalogues** are the supplementary path: when there is no spreadsheet, a layout-specific extractor converts the PDF to CSV first (`ingest/pdf_blum.py`).

---

## 2. What the Mapping Config Looks Like

```yaml
vendor: blum
source_system: blum_catalogue

defaults:                 # fill in values the file does not have
  category_code: hardware.hinge
  unit: ea
  currency: AUD
  brand: Blum
  supplier: Blum

extra_headers: attributes # unmapped columns → attributes (a supplier adding a column won't break it)

fields:
  - key: part_no          # used as {part_no} in templates
    target: identifier    # identifier / attribute / measure / column
    kind: supplier_code
    required: true
    aliases: ["Part no.", "Article no.", "Artikelnummer"]   # any number of supplier spellings
  - key: angle
    target: measure
    measure_key: opening_angle_deg
    unit: deg
    transform: number     # '110°' → 110
    aliases: ["Opening angle", "Öffnungswinkel"]

name_template: "{hinge} {boss} hinge {colour}"
family:
  key: [hinge, angle, application, colour]   # family key (same supplier, same material)
  axis: [boss]                               # variant axis
  name_template: "{brand} {hinge} {angle} {application} {colour}"
```

**Header matching** (`ingest/mapping.py`):

1. Exact match after normalization (case / whitespace / punctuation / BOM all flattened);
2. Containment match (`unitprice($)` ↔ `unit price`);
3. difflib similarity ≥ 0.82;
4. Still no match → handled per `extra_headers` (ignored by default, can be set to `attributes`).

When a required column does not match, it **fails outright and lists the actual headers**; it never imports the wrong data silently:

```
[blum] required column not matched: ['part_no']
  actual headers: [...]
  add it to aliases in mappings/blum.yaml
```

---

## 3. Idempotency and IDs

- Use `(identifier.kind, value, source_system)` to decide whether a record already exists: a hit means update, not create.
- On create, `material_uid` / `family_id` are generated from `sha256(source:value)` encoded as Crockford Base32, so **the same seed always yields the same ID** — repeated imports, or imports on another machine, never produce a second record.
- Prices go into `material_price` (the current price is only a cache in `material.unit_cost`); measures go into `material_measure` (only one preferred row per `(material, metric)`).
- `material_revision` / `material_event` are never written by hand: triggers in the database write them uniformly, so import cannot bypass auditing.

---

## 4. Measured Results

Real data extracted from catalogue pages 80–95 (the CLIP top hinge section):

```
$ python -m ingest.pdf_blum "Blum Catalogue ... .pdf" --pages 80-95
pages 80-95: 122 rows -> ingest/samples/blum_hinges.csv       (105 unique part numbers)

$ python scripts/import_file.py --file ingest/samples/blum_hinges.csv \
      --mapping mappings/blum.yaml --dsn $env:ML_DSN --apply
applied: {'new': 105, 'updated': 17, 'skipped': 0, 'measures': 89,
          'identifiers': 122, 'families': 122}

# run again (idempotency)
applied: {'new': 0, 'updated': 122, ...}
```

Results:

| Metric | Value |
|---|---|
| Material records | 109 (Blum 105 + Laminex 4) |
| Product families | 43 |
| Measures | 84 (`opening_angle_deg` 110/107/100/155°; `thickness_mm` 16/18/25) |
| Identifiers | 109 |
| Search `71B3590` | `exact`, 1 row |
| Search `clip top` | `and`, 40 rows |

**The family effect** (exactly the model you asked for): the same Laminex material at three thicknesses stays 3 records and 3 SKUs, but is one row from the family view:

```
Laminex Black MDF Melamine | members 3 | thickness 16~25 | price $38~$58
```

The Blum side is the same: the family `Blum CLIP top BLUMOTION Overlay NI` has `boss` as its variant axis (INSERTA / Screw-on / Knock-in).

---

## 5. Known Limitations

| Limitation | Detail | Handling |
|---|---|---|
| PDF extraction is layout-specific | Covers only the three-column "Order information" table (hinge section); drawer/runner sections have a different layout | One extractor per supplier per section, or ask for Excel first |
| Duplicate part numbers | The same part appears on multiple pages/columns → the later pass overwrites the earlier (`updated`) | Normal; add `source_ref` if provenance must be kept (page numbers already go into attributes) |
| Missing angle values | Some tables in the catalogue do not state an angle | Nulls allowed; the profile completeness score surfaces them later |
| No prices in the catalogue | The Blum catalogue contains no prices | Prices come from a separate price list (the `price` column is already reserved in the mapping) |
| Names may collide | Several parts in one column share the "CLIP top" heading | Name is not the key, `supplier_code` is; a spec suffix can be added later |

---

## 6. Next Steps

1. **Multiple sheets / multi-row headers**: price lists like Laminex's often have cover rows and merged cells, so `read_table` needs "skip the first N rows + specify the sheet".
2. **Import report**: `--json` can already export per-row results; add a "completeness distribution" (`v_material_completeness`) and "suspected duplicates" (`material_duplicate_candidate`, generated automatically).
3. **Separate the price list**: import the catalogue (product facts) and the price list (commercial facts) separately, join them on `(supplier, code)`, and give prices a `valid_from`.
4. **Generalize PDF extraction**: turn "three columns + Boss/Hinge/Spring/Colour/Part no." into configurable column anchors so other sections can reuse them.
