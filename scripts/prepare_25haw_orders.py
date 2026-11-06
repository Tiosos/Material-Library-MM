#!/usr/bin/env python3
"""Prepare the 25HAW ORDERS workbook for the Material Library B9 importer.

The committed sample is redacted by default. Prices, people, project name and
location, attachment references, order numbers, stock figures and dates are
dropped. Supplier names and product codes stay because they are the identifier
and the deduplication key of the material master records.

Derived columns:

* ``ThicknessMM`` / ``SheetLenMM`` / ``SheetWidMM`` - parsed from an
  ``A x B x C`` size triple in the description.
* ``FamilyBase`` - size-free family key, prefixed by the supplier, so the
  19 / 26 / 33 mm variants of the same board collapse into one family.

Usage::

    python scripts/prepare_25haw_orders.py \
        --source "25HAW ORDERS.xlsx" \
        --out ingest/samples/25haw_orders_redacted.csv

    # private local archive, never commit:
    python scripts/prepare_25haw_orders.py --source ... --out /tmp/full.csv \
        --include-sensitive

Nothing in the source workbook is modified.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import load_workbook

EXCEL_EPOCH = date(1899, 12, 30)

ALL_HEADERS = [
    "PCode", "ItemDescription", "Supplier", "Brand", "CategoryCode",
    "ThicknessMM", "SheetLenMM", "SheetWidMM", "FamilyBase",
    "UnitCost", "CostSub", "QTY", "StockTotal", "Location", "DocPDF",
    "Project", "RequestedBy", "DateRequired", "DateOrdered", "DateDue",
    "DateArrived", "Active", "TagSelect", "Priority", "OrderNo",
    "CutlistNo", "PhotoRef", "PCodeRaw", "SourceRow",
]

# Columns removed from the public sample: commercial terms, people, project
# details, internal references, procurement flow and stock figures.
REDACT_DROP = {
    "UnitCost", "CostSub", "QTY", "StockTotal", "Location", "DocPDF",
    "Project", "RequestedBy", "DateRequired", "DateOrdered", "DateDue",
    "DateArrived", "Active", "TagSelect", "Priority", "OrderNo",
    "CutlistNo", "PhotoRef",
}

EXCLUDE_KEYWORDS = ("delivery fee", "surcharge", "deposit", "parcel to ")

BRANDS = [
    "Blum", "Hettich", "Hafele", "Polytec", "Laminex", "Autex", "Surteco",
    "Timberwood Panels", "MAS", "Hailo", "Ninka", "Rubbermaid", "Lo and Co",
    "KEGWORKS", "Wilson & Bradley", "Lincoln Sentry", "Eurofit",
    "Elegant Hardware", "Ideal Bathroom", "Abbin Engineering", "Schiavello",
    "Arrow Wood", "Bord Products", "Barton Glass", "Trent Refrigeration",
    "Avo Studio", "RJ Cox",
]

SIZED_CATEGORIES = {
    "board", "board.melamine.mdf", "board.melamine.particle",
    "board.laminate", "benchtop", "glass", "edging",
}

SIZE_TRIPLE = re.compile(
    r"(?<![\d.])(\d{2,4})\s*[xX\u00d7]\s*(\d{2,4})\s*[xX\u00d7]\s*(\d{1,3})"
    r"(?!\s*[xX\u00d7])"
)


def text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    return re.sub(r"\s+", " ", str(v)).strip()


def iso(v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (int, float)):
        return (EXCEL_EPOCH + timedelta(days=int(v))).isoformat()
    return str(v).strip()


def num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_num(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def derive_brand(desc: str) -> str:
    d = desc.lower()
    for b in BRANDS:
        if b.lower() in d:
            return b
    return ""


def derive_category(desc: str, doc: str) -> str:
    """Best-effort classification into the seeded material_category tree."""
    d = re.sub(r"\s+", "", desc.upper())
    docu = (doc or "").upper()

    if "UNGLUED" in d:
        return "edging"
    if "FLOAT" in d:
        return "glass"
    if "BENCHTOP" in docu:
        return "benchtop"
    if "HINGE" in d:
        return "hardware.hinge"
    if "RUNNER" in d or "LEGRABOX" in d:
        return "hardware.runner"
    if "PULL" in d or "HANDLE" in d or "RACK" in d:
        return "hardware.handle"
    if "MDF" in d or "MELAMINE" in d:
        return "board.melamine.mdf"
    if "LDF" in d or "PARTICLE" in d or re.search(r"\bPB\b", desc.upper()):
        return "board.melamine.particle"
    if "LAMINATE" in d:
        return "board.laminate"
    if "ACOUSTIC" in d or "PANEL" in d:
        return "board"
    if any(k in d for k in ("CUSHION", "SEATING", "FRAMEWORK", "FOOTREST", "FRAME", "STAINLESS")):
        return "custom"
    if any(k in d for k in ("WASTE", "BIN", "ICEWELL", "MIRROR", "CABLETRAY", "TROLLEY",
                             "GLASSWASHER", "DOLLY", "SLIMJIM", "KITCHENITEMS", "REFRIGERATION")):
        return "appliance"
    if any(k in d for k in ("SHELF", "BOLT", "PLATE", "SUPPORT", "SCREW", "LOCKHOUSING")):
        return "hardware"
    return "generic"


def parse_size(desc: str, category: str):
    """Return (len, wid, thickness, span) from the first A x B x C triple."""
    if category not in SIZED_CATEGORIES:
        return "", "", "", None
    m = SIZE_TRIPLE.search(desc)
    if not m:
        return "", "", "", None
    a, b, c = (float(m.group(i)) for i in (1, 2, 3))
    return fmt_num(a), fmt_num(b), fmt_num(c), (m.start(), m.end())


def build_family_base(supplier: str, desc: str, span) -> str:
    if span is None:
        return ""
    base = desc[: span[0]] + desc[span[1]:]
    base = re.sub(r"^\s*Pcs:\s*\(?\d+\)?\s*", "", base, flags=re.I)
    base = re.sub(r"^[\s\-:,;]+", "", base)
    base = re.sub(r"\s+", " ", base).strip()
    if not base:
        return ""
    return f"{supplier} | {base}" if supplier else base


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="25HAW ORDERS.xlsx", help="source .xlsx path")
    ap.add_argument("--out", default="25haw_orders_redacted.csv", help="output .csv path")
    ap.add_argument("--include-sensitive", action="store_true",
                    help="keep every source column (private local archive only, never commit)")
    ap.add_argument("--summary", help="optional JSON summary path")
    args = ap.parse_args()

    src = Path(args.source)
    wb = load_workbook(src, read_only=True, data_only=True)
    ws = wb.active
    raw = list(ws.iter_rows(values_only=True))
    wb.close()

    src_headers = [str(h).strip() if h is not None else "" for h in raw[0][:20]]
    col = {h: i for i, h in enumerate(src_headers)}

    def get(row, name):
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else None

    kept: list[dict] = []
    excluded = Counter()
    seen_codes: Counter = Counter()
    duplicate_details: list[dict] = []

    for excel_row, row in enumerate(raw[1:], start=2):
        if row is None or all(v is None for v in row):
            continue
        desc = text(get(row, "_BoardGroup"))
        pcode = text(get(row, "_PCode"))
        supplier = text(get(row, "Supplier"))
        doc = text(get(row, "Doc_PDF"))

        if not desc:
            excluded["blank_description"] += 1
            continue
        if any(k in desc.lower() for k in EXCLUDE_KEYWORDS):
            excluded["fee_or_nonmaterial"] += 1
            continue
        if not pcode:
            excluded["missing_code"] += 1
            continue

        seen_codes[pcode] += 1
        ident = pcode if seen_codes[pcode] == 1 else f"{pcode}#{seen_codes[pcode]}"
        if seen_codes[pcode] > 1:
            duplicate_details.append({"excel_row": excel_row, "code": pcode, "identifier": ident})

        cost = num(get(row, "_Cost_Sub"))
        qty = num(get(row, "QTY"))
        if cost is None:
            unit_cost = ""
        elif qty and qty > 0:
            unit_cost = round(cost / qty, 4)
        else:
            unit_cost = round(cost, 4)

        category = derive_category(desc, doc)
        sheet_len, sheet_wid, thickness, span = parse_size(desc, category)

        kept.append({
            "PCode": ident,
            "ItemDescription": desc,
            "Supplier": supplier,
            "Brand": derive_brand(desc),
            "CategoryCode": category,
            "ThicknessMM": thickness,
            "SheetLenMM": sheet_len,
            "SheetWidMM": sheet_wid,
            "FamilyBase": build_family_base(supplier, desc, span),
            "UnitCost": unit_cost,
            "CostSub": "" if cost is None else round(cost, 4),
            "QTY": "" if qty is None else fmt_num(qty),
            "StockTotal": text(get(row, "T07b_ORD_StockControl::SumTotla")),
            "Location": text(get(row, "Location")),
            "DocPDF": doc,
            "Project": text(get(row, "_ProjectName")),
            "RequestedBy": text(get(row, "_RequestedByName")),
            "DateRequired": iso(get(row, "Date_Required")),
            "DateOrdered": iso(get(row, "Date_Ordered")),
            "DateDue": iso(get(row, "Date_Due")),
            "DateArrived": iso(get(row, "Date_Arrived")),
            "Active": text(get(row, "TAG_Active")),
            "TagSelect": text(get(row, "_Tag_Select")),
            "Priority": text(get(row, "_Priority")),
            "OrderNo": text(get(row, "OrderNumber")),
            "CutlistNo": text(get(row, "CutlistNumber")),
            "PhotoRef": text(get(row, "T07j_EMP_Main::PhotoThumNail")),
            "PCodeRaw": pcode,
            "SourceRow": excel_row,
        })

    headers = ALL_HEADERS if args.include_sensitive else [
        h for h in ALL_HEADERS if h not in REDACT_DROP
    ]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept)

    summary = {
        "source": str(src),
        "sheet": getattr(ws, "title", "Sheet1"),
        "source_headers": src_headers,
        "source_data_rows": len(raw) - 1,
        "kept": len(kept),
        "excluded": dict(excluded),
        "redacted": not args.include_sensitive,
        "output_columns": headers,
        "dropped_columns": [] if args.include_sensitive else sorted(REDACT_DROP),
        "duplicate_codes": {c: n for c, n in seen_codes.items() if n > 1},
        "duplicate_details": duplicate_details,
        "categories": dict(Counter(r["CategoryCode"] for r in kept)),
        "rows_with_size": sum(1 for r in kept if r["ThicknessMM"]),
        "rows_with_family": sum(1 for r in kept if r["FamilyBase"]),
        "output": str(out),
    }
    if args.summary:
        Path(args.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"source headers ({len(src_headers)}): {src_headers}")
    print(f"source data rows: {len(raw) - 1}")
    print(f"kept: {len(kept)}  excluded: {dict(excluded)}")
    print(f"redacted: {summary['redacted']}  output columns: {len(headers)}")
    print(f"rows with size: {summary['rows_with_size']}  rows with family: {summary['rows_with_family']}")
    print(f"duplicate codes: {summary['duplicate_codes']}")
    print(f"output -> {out}")


if __name__ == "__main__":
    main()
