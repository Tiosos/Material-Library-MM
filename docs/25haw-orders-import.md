# 25HAW ORDERS import (B9)

A worked example of turning a joinery procurement worksheet into UMR v2
material master data with the B9 importer, including physical size measures
and product families.

## Scope and data source

`25HAW ORDERS.xlsx` is an internal order sheet: one worksheet, 20 columns,
87 data rows. 71 rows are real material lines. The other 16 are delivery
fees, surcharges, deposits, parcel charges or blank descriptions, and are
excluded before import.

The dataset is **redacted for public consumption**. Only 11 columns remain:

| Column | Purpose |
| --- | --- |
| `PCode` | supplier product code, used as the primary identifier |
| `ItemDescription` | material name |
| `Supplier` | supplier party name |
| `Brand` | brand party name |
| `CategoryCode` | UMR category code |
| `ThicknessMM`, `SheetLenMM`, `SheetWidMM` | parsed physical measures |
| `FamilyBase` | size-free family key, prefixed by supplier |
| `PCodeRaw` | original code as text |
| `SourceRow` | source worksheet row number, for traceability |

Dropped entirely: unit and subtotal costs, quantities and stock totals,
project name and location, requester names, all four order dates, order and
cutlist numbers, order status and priority tags, attachment and photo
references, and the active/tag flags.

Supplier names and product codes are **kept**. They are the `supplier_code`
identifier and the deduplication key of the material master records, and
removing them would make idempotent re-imports impossible.

## Preparing the CSV

```bash
pip install openpyxl
python scripts/prepare_25haw_orders.py \
    --source "25HAW ORDERS.xlsx" \
    --out ingest/samples/25haw_orders_redacted.csv \
    --summary /tmp/25haw_summary.json
```

The committed sample is the default redacted output. `--include-sensitive`
keeps every source column; use it only for a private local archive and
**never commit the result**. Nothing in the source workbook is modified.

### Size parsing

`scripts/prepare_25haw_orders.py` looks for the first `A x B x C` size triple
in the description and maps it to `SheetLenMM`, `SheetWidMM` and
`ThicknessMM`. Parsing is restricted to sheet-goods categories (`board`,
`board.melamine.mdf`, `board.melamine.particle`, `board.laminate`,
`benchtop`, `glass`, `edging`) so that unrelated numbers in hardware
descriptions are not mistaken for dimensions. 32 of the 71 rows yield a size
triple; the remaining rows keep empty measure cells.

### Family key

`FamilyBase` is the description with the size triple removed, normalised and
prefixed by the supplier. Thickness is the variant axis, so different
thicknesses of the same product collapse into one `material_family`. For
example, the 19 / 26 / 33 mm Abstract Bronze MDF boards share one family key.

## Mapping

`mappings/25haw_orders.yaml` maps the 11 columns to UMR fields:

- `target: identifier`, `kind: supplier_code` on `PCode`
- `target: column` on `name`, `supplier`, `brand`, `category_code`
- three `target: measure` fields with keys `thickness_mm`, `sheet_len_mm` and
  `sheet_wid_mm`, each with `unit: mm` (matching
  `material_profile.axis_options` in `schema/seed.sql`)
- `family.key: [family_base]`, `family.axis: [thickness]`

## Import

```bash
# dry-run: header matching, measures and family assignment, no database needed
python scripts/import_file.py \
    --file ingest/samples/25haw_orders_redacted.csv \
    --mapping mappings/25haw_orders.yaml

# apply
python scripts/import_file.py \
    --file ingest/samples/25haw_orders_redacted.csv \
    --mapping mappings/25haw_orders.yaml --dsn "$ML_DSN" --apply
```

## Verified result

Against a clean `ml` database loaded with `schema/material.sql`,
`schema/search.sql` and `schema/seed.sql`:

| Check | Result |
| --- | --- |
| Rows / matched headers | 71 / 11 of 11 |
| Errors / warnings | 0 / 39 |
| `material` | 71 |
| `material_identifier` | 71 |
| `material_measure` | 96 (32 each of thickness, sheet length, sheet width) |
| `material_family` | 23 families, 32 members, axis `thickness_mm` |
| `material_price` | 0 (no cost data imported) |
| `party` | 30 |
| Idempotent re-run | `new=0, updated=71`, table counts unchanged |
| Search | `blum` 11 hits, `polytec` 16 hits, `abstract bronze` 6 hits |

The 39 warnings are expected: rows without a parsed size triple have an
empty `FamilyBase` and are deliberately skipped by family grouping.

## Privacy note

This sample is derived from a private order sheet. Every field that could
identify a project, a person, an address, an order, or a price has been
removed by deleting the whole column. Re-run the redaction scan before
committing any refreshed sample.
