"""Price-list import: commercial facts joined to an existing catalogue by (supplier, code).

The B9 design (docs/import-pipeline-b9.md section 6, item 3) separates product facts from
commercial facts: the catalogue import writes materials/identifiers/measures; a price list writes
`material_price` rows with an explicit `valid_from`, so price history is kept instead of being
silently overwritten by "today's price".

Join rule (read-only lookup before any write):

    party (canonical_name OR alias = supplier, kind in supplier/both) -> supplier_id
    material_identifier (value = code, source_system = given)         -> material_uid
    material.default_supplier_id must equal supplier_id                (guards a same-code clash
                                                                        across two suppliers)

No material rows are created here. A code that is not in the catalogue is reported as unmatched,
never inserted: a price list must not invent product records. Writes are idempotent on the table's
unique key (material_uid, supplier_id, valid_from, qty_break, cost_basis) with ON CONFLICT DO NOTHING.
The `material.unit_cost` cache is refreshed from the newest price after a successful batch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .mapping import norm, to_number

# Roles in a price list. Aliases are normalized (lowercase, punctuation flattened).
CODE_ALIASES = (
    "code", "productcode", "partno", "partnumber", "sku", "itemno", "itemnumber",
    "articleno", "artikelnummer", "materialcode", "pcode", "pcoderaw", "catno",
)
SUPPLIER_ALIASES = ("supplier", "vendor", "lieferant", "suppliername", "manufacturer", "marke")
PRICE_ALIASES = (
    "price", "unitprice", "listprice", "tradeprice", "cost", "unitcost",
    "priceperunit", "pricepersheet", "preis", "einzelpreis",
)
CURRENCY_ALIASES = ("currency", "curr", "waehrung", "währung", "currencyname")
BASIS_ALIASES = ("costbasis", "basis", "taxbasis", "pricetype", "basisofprice")
QTY_ALIASES = ("qtybreak", "quantitybreak", "qty", "minimumqty", "minqty", "staffel")
DATE_ALIASES = ("validfrom", "effectivedate", "effectivefrom", "date", "gueltigab", "gültigab")


def _match(header: str, aliases: Iterable[str]) -> bool:
    n = norm(header)
    return any(a == n or a in n for a in aliases)


@dataclass
class PriceDraft:
    row_no: int
    code: str = ""
    supplier: str = ""
    unit_cost: float | None = None
    currency: str = "AUD"
    cost_basis: str = "ex_gst"
    qty_break: float = 0.0
    valid_from: str | None = None
    material_uid: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def resolve_price_columns(headers: list[str]) -> dict[str, str | None]:
    """Map each role to its header. Supplier and code are optional at this stage: a CLI default
    can supply either (common for a single-vendor price list)."""
    found: dict[str, str | None] = {}
    for role, aliases in (
        ("code", CODE_ALIASES), ("supplier", SUPPLIER_ALIASES), ("price", PRICE_ALIASES),
        ("currency", CURRENCY_ALIASES), ("cost_basis", BASIS_ALIASES),
        ("qty_break", QTY_ALIASES), ("valid_from", DATE_ALIASES),
    ):
        found[role] = next((h for h in headers if _match(h, aliases)), None)
    return found


def build_price_rows(
    headers: list[str],
    rows: list[dict],
    *,
    supplier_default: str | None = None,
    valid_from_default: str | None = None,
    currency_default: str = "AUD",
    cost_basis_default: str = "ex_gst",
    code_column: str | None = None,
) -> tuple[list[PriceDraft], dict]:
    """Pure: turn a price table into drafts. No DB. Per-row valid_from wins over the default."""
    cols = resolve_price_columns(headers)
    if code_column:
        matches = [h for h in headers if h == code_column or norm(h) == norm(code_column)]
        if not matches:
            raise ValueError(f"--code-column {code_column!r} not in headers: {headers}")
        cols["code"] = matches[0]
    if cols["price"] is None:
        raise ValueError("no price column found; headers: " + ", ".join(headers))

    drafts: list[PriceDraft] = []
    for i, row in enumerate(rows, start=2):
        d = PriceDraft(row_no=i)
        raw_code = row.get(cols["code"]) if cols["code"] else None
        d.code = str(raw_code).strip() if raw_code not in (None, "") else ""
        raw_supplier = row.get(cols["supplier"]) if cols["supplier"] else None
        d.supplier = str(raw_supplier).strip() if raw_supplier not in (None, "") else (supplier_default or "")
        d.valid_from = (str(row.get(cols["valid_from"])).strip()[:10]
                        if cols["valid_from"] and row.get(cols["valid_from"]) not in (None, "")
                        else valid_from_default)
        d.currency = (str(row.get(cols["currency"])).strip().upper()
                      if cols["currency"] and row.get(cols["currency"]) not in (None, "")
                      else currency_default)
        d.cost_basis = (str(row.get(cols["cost_basis"])).strip()
                        if cols["cost_basis"] and row.get(cols["cost_basis"]) not in (None, "")
                        else cost_basis_default)
        qty = row.get(cols["qty_break"]) if cols["qty_break"] else None
        d.qty_break = to_number(qty) or 0.0
        d.unit_cost = to_number(row.get(cols["price"]))

        if not d.code:
            d.errors.append("no product code")
        if not d.supplier:
            d.errors.append("no supplier (add a column or pass --supplier)")
        if d.valid_from is None:
            d.errors.append("no valid_from (add a column or pass --valid-from YYYY-MM-DD)")
        else:
            from datetime import date
            try:
                date.fromisoformat(d.valid_from)
            except ValueError:
                d.errors.append(f"invalid valid_from {d.valid_from!r} (expected YYYY-MM-DD)")
        if d.unit_cost is None:
            d.errors.append("price is not a number")
        elif d.unit_cost < 0:
            d.errors.append("price is negative")
        if len(d.currency) != 3 or not d.currency.isalpha():
            d.errors.append(f"invalid currency {d.currency!r}")
        if d.cost_basis not in {"ex_gst", "inc_gst", "ex_tax", "inc_tax"}:
            d.errors.append(f"invalid cost_basis {d.cost_basis!r}")
        drafts.append(d)

    meta = {"resolved": cols, "rows": len(drafts)}
    return drafts, meta


def price_report(drafts: list[PriceDraft], meta: dict) -> dict:
    return {
        "rows": len(drafts),
        "ok": sum(1 for d in drafts if not d.errors),
        "errors": sum(1 for d in drafts if d.errors),
        "resolved": meta["resolved"],
        "error_rows": [{"row": d.row_no, "errors": d.errors} for d in drafts if d.errors][:20],
    }


# ---------------------------------------------------------------------------
# DB: read-only lookup + idempotent writes
# ---------------------------------------------------------------------------
def _find_supplier(cur, name: str) -> int | None:
    cur.execute(
        """SELECT id FROM party
            WHERE lower(canonical_name)=lower(%s) AND kind IN ('supplier','both')
            LIMIT 1""",
        (name,),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT party_id FROM party_alias WHERE lower(alias)=lower(%s) LIMIT 1", (name,))
    row = cur.fetchone()
    return row[0] if row else None


def _find_material(cur, code: str, source_system: str, supplier_id: int) -> str | None:
    cur.execute(
        """SELECT m.material_uid
             FROM material_identifier mi
             JOIN material m ON m.material_uid=mi.material_uid
            WHERE mi.value=%s AND mi.source_system=%s
              AND (m.default_supplier_id=%s OR m.default_supplier_id IS NULL)
            ORDER BY mi.is_primary DESC, mi.kind
            LIMIT 1""",
        (code, source_system, supplier_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


def apply_prices(
    conn,
    drafts: list[PriceDraft],
    *,
    source_system: str,
    actor: str = "import:prices",
) -> dict:
    """Write price rows for matched drafts. Never creates materials. Returns stats."""
    stats = {"inserted": 0, "unchanged": 0, "unmatched_supplier": 0,
             "unmatched_code": 0, "skipped": 0, "cache_updated": 0}
    suppliers: dict[str, int | None] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('ml.actor', %s, true)", (actor,))
        cur.execute("SELECT set_config('ml.source', %s, true)", (source_system,))
        for d in drafts:
            if d.errors:
                stats["skipped"] += 1
                continue
            if d.supplier not in suppliers:
                suppliers[d.supplier] = _find_supplier(cur, d.supplier)
            supplier_id = suppliers[d.supplier]
            if supplier_id is None:
                stats["unmatched_supplier"] += 1
                d.warnings.append(f"supplier {d.supplier!r} not found in party")
                continue
            uid = _find_material(cur, d.code, source_system, supplier_id)
            if uid is None:
                stats["unmatched_code"] += 1
                d.warnings.append(f"code {d.code!r} not found for supplier {d.supplier!r}")
                continue
            d.material_uid = uid
            cur.execute(
                """INSERT INTO material_price
                     (material_uid, supplier_id, unit_cost, currency, valid_from,
                      qty_break, cost_basis, source_system)
                   VALUES (%s,%s,%s,%s,%s::date,%s,%s,%s)
                   ON CONFLICT (material_uid, supplier_id, valid_from, qty_break, cost_basis)
                   DO NOTHING""",
                (uid, supplier_id, d.unit_cost, d.currency, d.valid_from,
                 d.qty_break, d.cost_basis, source_system),
            )
            inserted = cur.rowcount > 0
            stats["inserted" if inserted else "unchanged"] += 1
            d.warnings.append(f"matched material {uid}")

        # Refresh the material.unit_cost cache from the newest price seen in this batch.
        newest: dict[str, tuple[str, float]] = {}
        for d in drafts:
            if d.errors or d.material_uid is None or d.valid_from is None or d.unit_cost is None:
                continue
            uid = d.material_uid
            if uid not in newest or d.valid_from > newest[uid][0]:
                newest[uid] = (d.valid_from, d.unit_cost)
        for uid, (batch_date, cost) in newest.items():
            # Only refresh the cache when this batch's date is the newest price on record.
            # A back-dated price list must not overwrite the current cached price.
            cur.execute("SELECT max(valid_from) FROM material_price WHERE material_uid=%s", (uid,))
            row = cur.fetchone()
            if row and row[0] and str(row[0])[:10] > batch_date:
                continue
            cur.execute("UPDATE material SET unit_cost=%s WHERE material_uid=%s", (cost, uid))
            stats["cache_updated"] += 1
    return stats
