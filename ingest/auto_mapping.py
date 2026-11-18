"""Generate a supplier mapping from CSV/XLSX headers, no YAML authoring required.

The explicit mapping format (ingest/mapping.py, schema/mapping.schema.json) stays the source of
truth. This module only *produces* a mapping dict from the observed headers and a few sample rows,
so a new supplier can be dry-run imported in one command instead of writing aliases first. Every
guess is reported: identifier is essential, the rest can be corrected by editing the saved YAML.

Rules, in match order (normalized header -> alias set):

  * code / sku / part no / artikelnummer / item no ... -> identifier (supplier_code), required
  * price / unit cost / list price / preis ...        -> column unit_cost (number)
  * name / description / bezeichnung ...              -> column name
  * brand / manufacturer / marke ...                  -> column brand
  * supplier / vendor / lieferant ...                 -> column supplier
  * category / kategorie /Typ ...                     -> column category_code
  * unit / uom / einheit ...                          -> column unit
  * currency / waehrung ...                           -> column currency
  * a numeric dimension (thickness, width, length, angle, diameter ...) with numeric sample
                                                      -> measure (key + UCUM unit inferred)
  * everything else                                   -> attribute

Unmatched columns are kept as attributes (extra_headers: attributes), so a supplier adding a
column cannot break the import. No `family` block is emitted: grouping is a judgement call and a
wrong guess is worse than none; the user can add one to the saved YAML.

The generated dict validates against schema/mapping.schema.json and scripts/check_mappings.py.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .mapping import norm, to_number

VENDOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Alias -> target role. Values are (target, extra spec). Keys are normalized (lowercase, no punctuation).
CODE_ALIASES = (
    "code", "partno", "partnumber", "sku", "itemno", "itemnumber", "articleno",
    "articlenumber", "artikelnummer", "materialno", "materialcode", "productcode",
    "pcode", "pcodraw", "catalogno", "catno", "modelno", "ident", "id", "reference",
    "ref", "barcode", "ean", "gtin",
)
PRICE_ALIASES = (
    "price", "unitprice", "listprice", "tradeprice", "cost", "unitcost",
    "pricepersheet", "preis", "einzelpreis", "verkaufspreis", "rrp",
)
NAME_ALIASES = (
    "name", "description", "itemdescription", "productname", "materialdescription",
    "bezeichnung", "beschreibung", "title", "product",
)
BRAND_ALIASES = ("brand", "manufacturer", "marke", "hersteller", "make")
SUPPLIER_ALIASES = ("supplier", "vendor", "lieferant", "suppliername", "source")
CATEGORY_ALIASES = ("category", "categorycode", "kategorie", "typ", "type", "group", "warengruppe")
UNIT_ALIASES = ("unit", "uom", "einheit", "unitofmeasure", "measureunit")
CURRENCY_ALIASES = ("currency", "waehrung", "währung", "curr", "currencyname")

# Dimension header -> (measure key suffix, UCUM unit). Number transform is applied by the importer.
DIMENSIONS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("thickness", "thicknessmm", "sheetthickness", "staerke", "dicke"), "thickness_mm", "mm"),
    (("width", "sheetwidmm", "breite"), "width_mm", "mm"),
    (("length", "sheetlenmm", "laenge"), "length_mm", "mm"),
    (("height", "hoehe"), "height_mm", "mm"),
    (("diameter", "dia", "durchmesser"), "diameter_mm", "mm"),
    (("openingangle", "angle", "oeffnungswinkel", "öffnungswinkel"), "opening_angle_deg", "deg"),
    (("weight", "gewicht"), "weight_kg", "kg"),
    (("leadtime", "leadtimedays", "lieferzeit"), "lead_time_days", "d"),
)

# Headers to ignore entirely (row metadata, not product data).
IGNORE_ALIASES = {"sourcerow", "rownumber", "rowid", "index", "seq", "lineno"}

DEFAULT_CATEGORY = "generic"
DEFAULT_CURRENCY = "AUD"


def _slug_key(header: str) -> str:
    """Field key from a header: lower snake_case, valid under the mapping schema."""
    s = re.sub(r"[^a-z0-9]+", "_", norm(header)).strip("_")
    if not s:
        s = "field"
    if s[0].isdigit():
        s = "f_" + s
    return s


def _in(header: str, aliases: Iterable[str]) -> bool:
    n = norm(header)
    return any(a == n or a in n for a in aliases)


def _dimension(header: str) -> tuple[str, str] | None:
    n = norm(header)
    for aliases, key, unit in DIMENSIONS:
        if any(a == n or a in n for a in aliases):
            return key, unit
    return None


def _sample_values(rows: list[dict], header: str, limit: int = 20) -> list[Any]:
    out = []
    for row in rows[:limit]:
        v = row.get(header)
        if v not in (None, ""):
            out.append(v)
    return out


def infer_mapping(
    headers: list[str],
    rows: list[dict],
    *,
    vendor: str = "supplier",
    source_system: str | None = None,
    category_code: str | None = None,
    unit: str | None = None,
    currency: str = DEFAULT_CURRENCY,
    brand: str | None = None,
    supplier: str | None = None,
    code_column: str | None = None,
) -> tuple[dict, dict]:
    """Return (mapping_dict, report). No DB needed; report explains every decision."""
    if not VENDOR_RE.match(vendor or ""):
        raise ValueError(f"vendor must match {VENDOR_RE.pattern}: {vendor!r}")
    if not headers:
        raise ValueError("file has no headers")

    fields: list[dict] = []
    used_keys: set[str] = set()
    report: dict[str, Any] = {
        "vendor": vendor, "headers": list(headers), "resolved": [], "unresolved": [],
        "warnings": [], "identifier": None, "category_code": category_code or DEFAULT_CATEGORY,
    }

    def add(header: str, target: str, role: str, **spec: Any) -> None:
        key = _slug_key(header)
        base = key
        suffix = 2
        while key in used_keys:
            key = f"{base}_{suffix}"
            suffix += 1
        used_keys.add(key)
        field = {"key": key, "target": target, "aliases": [header], **spec}
        fields.append(field)
        report["resolved"].append({"header": header, "field": key, "target": target, "role": role})

    # Pass 1: the identity column. An explicit --code-column wins; then an alias match; then
    # the first non-name/non-dimension column so a plain "Article" sheet still works (warned).
    code_header = None
    if code_column:
        matches = [h for h in headers if h == code_column or norm(h) == norm(code_column)]
        if not matches:
            raise ValueError(f"--code-column {code_column!r} not in headers: {headers}")
        code_header = matches[0]
    if code_header is None:
        code_header = next((h for h in headers if _in(h, CODE_ALIASES)), None)
    if code_header is None:
        candidates = [h for h in headers if _in(h, NAME_ALIASES) is False and not _dimension(h)]
        if candidates:
            code_header = candidates[0]
            report["warnings"].append(
                f"no explicit code header; using first data column {code_header!r} as the identifier"
            )
    if code_header is None:
        raise ValueError("cannot find an identifier column; pass --code-column <header>")
    add(code_header, "identifier", "identifier", kind="supplier_code", required=True)
    report["identifier"] = code_header

    # Pass 2: every other header. A dimension becomes a measure only when the sample values
    # are numeric - a text-only "Length description" stays an attribute.
    for h in headers:
        if h == code_header or not h.strip():
            continue
        if _in(h, IGNORE_ALIASES):
            report["unresolved"].append({"header": h, "reason": "ignored (row metadata)"})
            continue
        if _in(h, PRICE_ALIASES):
            add(h, "column", "unit_cost", column="unit_cost", transform="number")
        elif _in(h, NAME_ALIASES):
            add(h, "column", "name", column="name")
        elif _in(h, BRAND_ALIASES):
            add(h, "column", "brand", column="brand")
        elif _in(h, SUPPLIER_ALIASES):
            add(h, "column", "supplier", column="supplier")
        elif _in(h, CATEGORY_ALIASES):
            add(h, "column", "category_code", column="category_code")
        elif _in(h, UNIT_ALIASES):
            add(h, "column", "unit", column="unit")
        elif _in(h, CURRENCY_ALIASES):
            add(h, "column", "currency", column="currency")
        else:
            dim = _dimension(h)
            sample = _sample_values(rows, h)
            numeric = bool(sample) and all(to_number(v) is not None for v in sample)
            if dim and numeric:
                measure_key, measure_unit = dim
                add(h, "measure", measure_key, measure_key=measure_key,
                    unit=measure_unit, transform="number")
            else:
                add(h, "attribute", "attribute")

    defaults: dict[str, Any] = {
        "category_code": category_code or DEFAULT_CATEGORY,
        "currency": currency,
    }
    if unit:
        defaults["unit"] = unit
    if brand:
        defaults["brand"] = brand
    if supplier:
        defaults["supplier"] = supplier

    # Name template: prefer a real name column; otherwise identifier + first attribute.
    name_field = next((f["key"] for f in fields
                       if f["target"] == "column" and f.get("column") == "name"), None)
    if name_field:
        name_template = "{" + name_field + "}"
    else:
        first_attr = next((f["key"] for f in fields if f["target"] == "attribute"), None)
        code_key = fields[0]["key"]
        name_template = "{" + code_key + "}"
        if first_attr:
            name_template += " {" + first_attr + "}"

    mapping: dict[str, Any] = {
        "vendor": vendor,
        "source_system": source_system or vendor,
        "defaults": defaults,
        "extra_headers": "attributes",
        "fields": fields,
        "name_template": name_template,
    }
    report["name_template"] = name_template
    return mapping, report
