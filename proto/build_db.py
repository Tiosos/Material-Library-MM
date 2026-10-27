#!/usr/bin/env python3
"""生成原型数据库 materials.sqlite + materials.jsonl（UMR 交换格式示例）。

数据结构对齐 schema/material.sql，字段语义对齐 joineryflow catalog 9 表的真实字段
（board.glass.timber.hardware.edging.benchtop.appliance.custom.hire）。

用法:  python3 build_db.py
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "materials.sqlite"
JSONL_PATH = HERE / "data" / "materials.jsonl"

random.seed(20261027)

# 固定基准时间戳：保证每次重建得到完全相同的 uid 与 jsonl，
# 避免重新生成样例数据时产生无意义的 git diff。
BASE_TS_MS = 1_763_000_000_000

# --------------------------------------------------------------------------- #
# ULID 生成（无依赖，够用即可；生产环境用 ulid-py）
# --------------------------------------------------------------------------- #
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid(ts_ms: int | None = None) -> str:
    if ts_ms is None:
        ts_ms = BASE_TS_MS
    out = ""
    t = ts_ms
    for _ in range(10):
        out = _ALPHABET[t % 32] + out
        t //= 32
    for _ in range(16):
        out += _ALPHABET[random.randrange(32)]
    return "mat_" + out


# --------------------------------------------------------------------------- #
# 素材池（品牌取自 joineryflow seed 中的真实供应商：Laminex / Briggs Veneers /
# CDK Stone / Miele / Winning Appliances / Metalform / Kennards …）
# --------------------------------------------------------------------------- #
BOARDS = [
    # (brand, substrate, finish, colour, thickness, sheet_len, sheet_wid, price, supplier)
    ("Laminex", "MDF", "Melamine", "White", 16, 3600, 1200, 62.0, "Laminex Australia"),
    ("Laminex", "MDF", "Melamine", "Black", 16, 3600, 1200, 68.0, "Laminex Australia"),
    ("Laminex", "MDF", "Melamine", "Grey", 18, 3600, 1200, 74.0, "Laminex Australia"),
    ("Laminex", "MR MDF", "Melamine", "White", 18, 3600, 1200, 96.0, "Laminex Australia"),
    ("Laminex", "Particleboard", "Melamine", "White", 16, 3600, 1800, 58.0, "Laminex Australia"),
    ("Laminex", "Particleboard", "Raw", "Natural", 33, 3600, 1800, 88.0, "Laminex Australia"),
    ("Polytec", "MDF", "Melamine", "Oak", 18, 3600, 1200, 82.0, "Polytec"),
    ("Polytec", "MDF", "Polyurethane", "White", 18, 3600, 1200, 145.0, "Polytec"),
    ("Polytec", "MDF", "Vinyl", "Charcoal", 16, 3600, 1200, 79.0, "Polytec"),
    ("Briggs Veneers", "Plywood", "Veneer", "American Oak", 19, 2400, 1200, 210.0, "Briggs Veneers"),
    ("Briggs Veneers", "Plywood", "Veneer", "Walnut", 19, 2400, 1200, 265.0, "Briggs Veneers"),
    ("Briggs Veneers", "MDF", "Veneer", "Blackbutt", 25, 2400, 1200, 232.0, "Briggs Veneers"),
    ("Borg", "Particleboard", "Melamine", "Walnut", 16, 3600, 1800, 71.0, "Borg Manufacturing"),
    ("Borg", "MDF", "Melamine", "White", 9, 2400, 1200, 44.0, "Borg Manufacturing"),
    ("Formica", "MDF", "Laminate", "Navy", 18, 3600, 1200, 118.0, "Formica Group"),
    ("Laminex", "MDF", "Raw", "Natural", 32, 3600, 1200, 126.0, "Laminex Australia"),
    ("Polytec", "MR MDF", "Melamine", "White", 16, 3600, 1200, 92.0, "Polytec"),
    ("Laminex", "Plywood", "Raw", "Pine", 25, 2400, 1200, 118.0, "Laminex Australia"),
]

HARDWARE = [
    ("Blum", "Blumotion Soft Close Hinge 107° Clip Top", "hinge", "ea", 12.40, "Blum Australia"),
    ("Blum", "Blumotion Soft Close Hinge 110° Clip Top Blum", "hinge", "ea", 13.80, "Blum Australia"),
    ("Blum", "Tandembox Antaro Drawer Runner 400mm", "drawer runner", "ea", 46.0, "Blum Australia"),
    ("Blum", "Tandembox Antaro Drawer Runner 500mm Blumotion", "drawer runner", "ea", 52.0, "Blum Australia"),
    ("Blum", "Legrabox Drawer Side Set 450mm", "drawer runner", "set", 168.0, "Blum Australia"),
    ("Blum", "Aventos HK Top Lift System", "lift system", "ea", 214.0, "Blum Australia"),
    ("Blum", "Clip Top Hinge Plate 0mm Cruciform", "hinge plate", "ea", 3.2, "Blum Australia"),
    ("Blum", "TIP-ON Push to Open for Tandembox", "push open", "ea", 28.0, "Blum Australia"),
    ("Hettich", "InnoTech Atira Drawer Runner 450mm", "drawer runner", "ea", 42.0, "Hettich Australia"),
    ("Hettich", "Sensys Soft Close Hinge 110°", "hinge", "ea", 11.6, "Hettich Australia"),
    ("Hettich", "Push to Open Silent System", "push open", "ea", 24.0, "Hettich Australia"),
    ("Häfele", "Moovit Drawer Runner 400mm Soft Close", "drawer runner", "ea", 38.0, "Hafele Australia"),
    ("Häfele", "Dialock Electronic Lock", "lock", "ea", 265.0, "Hafele Australia"),
    ("Häfele", "LED Strip Light 3000K 5m", "lighting", "ea", 88.0, "Hafele Australia"),
    ("Grass", "Nova Pro Scala Drawer Runner 500mm", "drawer runner", "ea", 58.0, "Grass Australia"),
    ("Kethy", "Bar Handle 160mm Brushed Nickel", "handle", "ea", 12.0, "Kethy"),
    ("Kethy", "Bar Handle 320mm Matte Black", "handle", "ea", 18.0, "Kethy"),
    ("Kethy", "Knob 32mm Antique Brass", "handle", "ea", 6.4, "Kethy"),
    ("Blum", "Shelf Pin 5mm Nickel 100pk", "shelf support", "box", 9.8, "Blum Australia"),
    ("Hettich", "Cam Lock 15mm 100pk", "connector", "box", 14.0, "Hettich Australia"),
    ("Hettich", "Wooden Dowel 8x30mm 500pk", "connector", "box", 22.0, "Hettich Australia"),
    ("Generic", "Screw 4x40mm CSK Zinc 500pk", "fastener", "box", 26.0, "Bunnings Trade"),
    ("Generic", "Screw 3.5x16mm CSK Zinc 1000pk", "fastener", "box", 18.0, "Bunnings Trade"),
    ("Generic", "Confirmat Screw 7x50mm 200pk", "fastener", "box", 34.0, "Bunnings Trade"),
]

EDGING = [
    ("Rehau", "ABS Edging Tape 2mm x 22mm White", "ABS", "White", 2.0, 22, 75, 0.95, "Rehau Australia"),
    ("Rehau", "ABS Edging Tape 2mm x 33mm Black", "ABS", "Black", 2.0, 33, 75, 1.15, "Rehau Australia"),
    ("Rehau", "ABS Edging Tape 1mm x 22mm White", "ABS", "White", 1.0, 22, 150, 0.55, "Rehau Australia"),
    ("Rehau", "ABS Edging Tape 1mm x 45mm Grey", "ABS", "Grey", 1.0, 45, 150, 0.72, "Rehau Australia"),
    ("Dollar", "PVC Edging Tape 2mm x 22mm Natural Oak", "PVC", "Natural Oak", 2.0, 22, 100, 0.88, "Dollar Australia"),
    ("Dollar", "PVC Edging Tape 0.4mm x 22mm Walnut", "PVC", "Walnut", 0.4, 22, 200, 0.31, "Dollar Australia"),
    ("Briggs Veneers", "Veneer Edging Tape 0.6mm x 45mm American Oak", "Veneer", "American Oak", 0.6, 45, 50, 2.4, "Briggs Veneers"),
    ("Rehau", "ABS Edging Tape 2mm x 45mm Black", "ABS", "Black", 2.0, 45, 75, 1.42, "Rehau Australia"),
]

GLASS = [
    ("Viridian", "Toughened Safety Glass 6mm Clear", "Toughened", 6, "Clear", 3.9),
    ("Viridian", "Toughened Safety Glass 10mm Clear", "Toughened", 10, "Clear", 6.2),
    ("Viridian", "Laminated Safety Glass 6.38mm Clear", "Laminated", 6.38, "Clear", 5.4),
    ("Viridian", "Frosted Glass 6mm Satinlite", "Frosted", 6, "Satinlite", 7.1),
    ("Viridian", "Mirror 6mm Silver Clear", "Mirror", 6, "Silver", 8.6),
    ("Viridian", "Grey Tinted Glass 8mm Grey", "Tinted", 8, "Grey", 9.4),
    ("Viridian", "Low-E Glass 6mm ComfortPlus", "Low-E", 6, "Clear", 12.8),
    ("AGC", "Toughened Splashback 6mm Ultra White", "Toughened", 6, "Ultra White", 11.2),
]

TIMBER = [
    ("Australian Hardwood", "Tasmanian Oak 19mm DAR", "Tasmanian Oak", 19, 90, 2400),
    ("Australian Hardwood", "Tasmanian Oak 25mm DAR", "Tasmanian Oak", 25, 140, 2400),
    ("Australian Hardwood", "Blackbutt 19mm DAR", "Blackbutt", 19, 90, 2100),
    ("Australian Hardwood", "Spotted Gum 32mm DAR", "Spotted Gum", 32, 180, 2400),
    ("Australian Hardwood", "Victorian Ash 38mm DAR", "Victorian Ash", 38, 190, 2400),
    ("Briggs Veneers", "American Oak 25mm Select Grade", "American Oak", 25, 150, 2400),
    ("Briggs Veneers", "American Walnut 19mm Select Grade", "American Walnut", 19, 120, 2100),
    ("Bunnings Trade", "Radiata Pine 90x45 F7", "Radiata Pine", 45, 90, 2700),
]

BENCHTOP = [
    ("Caesarstone", "Caesarstone 6131 Bianco Drift 20mm", "Engineered Stone", 20, "CDK Stone", 1450.0),
    ("Caesarstone", "Caesarstone 4033 Rugged Concrete 20mm", "Engineered Stone", 20, "CDK Stone", 1380.0),
    ("Caesarstone", "Caesarstone 5100 Empira Black 20mm", "Engineered Stone", 20, "CDK Stone", 1620.0),
    ("Silestone", "Silestone Eternal Calacatta Gold 20mm", "Engineered Stone", 20, "CDK Stone", 1580.0),
    ("Silestone", "Silestone Blanco City 20mm", "Engineered Stone", 20, "CDK Stone", 1240.0),
    ("Quantum Quartz", "Quantum Quartz 5000 Carrara 20mm", "Engineered Stone", 20, "WK Stone", 1120.0),
    ("Corian", "Corian Glacier White 12mm Solid Surface", "Solid Surface", 12, "CDK Stone", 980.0),
    ("Laminex", "Laminate Benchtop 33mm Squareform White", "Laminate", 33, "Laminex Australia", 320.0),
    ("Polytec", "Laminate Benchtop 33mm Classic Oak", "Laminate", 33, "Polytec", 340.0),
]

APPLIANCE = [
    ("Miele", "Miele 60cm Oven H7164BP", "Winning Appliances", 3450.0),
    ("Miele", "Miele 60cm Combi Steam Oven DGC7460", "Winning Appliances", 5290.0),
    ("Miele", "Miele Integrated Dishwasher G7365", "Winning Appliances", 2890.0),
    ("Bosch", "Bosch 60cm Oven HBG6764S6A", "Bosch Australia", 2340.0),
    ("Bosch", "Bosch 90cm Gas Cooktop PXY975DC1E", "Bosch Australia", 1890.0),
    ("Fisher & Paykel", "Fisher & Paykel 90cm Oven OB90S9MEPX3", "Fisher & Paykel", 3190.0),
    ("Schweigen", "Schweigen 90cm Undermount Rangehood UM1170", "Schweigen", 2280.0),
    ("Zip", "Zip HydroTap G4 Boiling Chilled Sparkling", "Zip Water", 4250.0),
    ("Smeg", "Smeg 45cm Compact Oven SFA4101VCX", "Smeg Australia", 1980.0),
]

CUSTOM = [
    ("Metalform", "Bespoke signage box (reception)", 1250.0, 14),
    ("Metalform", "Custom powdercoated steel bracket set", 480.0, 10),
    ("Austmont", "Custom aluminium reveal angle 40x40", 320.0, 12),
    ("Joinery Co", "Contractor manufactured wardrobe carcass set", 2400.0, 21),
]

HIRE = [
    ("Kennards Hire", "Scaffold tower 4m", 180.0, "week"),
    ("Kennards Hire", "Laser level rotary", 95.0, "day"),
    ("Coates Hire", "Dust extractor M class", 210.0, "week"),
    ("Coates Hire", "Panel lifter vacumate", 140.0, "day"),
]

CONSUMABLE = [
    ("Selleys", "Aquadhere PVA Wood Glue 500ml", "adhesive", "L", 12.5, "Bunnings Trade"),
    ("Selleys", "Aquadhere PVA Wood Glue 4L", "adhesive", "L", 46.0, "Bunnings Trade"),
    ("Titebond", "Titebond III Ultimate Wood Glue 1L", "adhesive", "L", 32.0, "Bunnings Trade"),
    ("Wattyl", "Wattyl Estapol Satin Clear 1L", "coating", "L", 42.0, "Wattyl"),
    ("Wattyl", "Wattyl Primer Undercoat 4L", "coating", "L", 68.0, "Wattyl"),
    ("Dulux", "Dulux Aquanamel Semi Gloss White 1L", "coating", "L", 54.0, "Dulux"),
    ("3M", "3M Double Sided Tape 25mm x 50m", "tape", "roll", 18.0, "Bunnings Trade"),
    ("Sika", "Sika Silicone Sealant White 300ml", "sealant", "ea", 11.4, "Bunnings Trade"),
]

# 俗称 / 别名表：这是"缺省搜索"能兜住错拼和俗称的关键
SYNONYM_HINTS = {
    "melamine": ["laminate board", "melamine board", "malamine", "melimine"],
    "mdf": ["medium density fibreboard", "medium density fiberboard", "中纤板", "密度板"],
    "plywood": ["ply", "multiplex", "胶合板"],
    "hinge": ["hinges", "butt hinge", "concealed hinge", "铰链"],
    "drawer runner": ["drawer slide", "runner", "glide", "drawer slide rail", "导轨"],
    "edging": ["edge tape", "edgebanding", "abs tape", "封边"],
    "benchtop": ["countertop", "worktop", "台面"],
    "engineered stone": ["caesarstone", "silestone", "quartz", "人造石"],
    "toughened": ["tempered", "safety glass", "钢化玻璃"],
    "handle": ["pull", "knob", "拉手"],
    "screw": ["fastener", "wood screw", "螺丝"],
    "glue": ["adhesive", "pva", "胶水"],
}


def _hints(*parts: str) -> list[str]:
    """按词表给一条材料补同义词。"""
    out: list[str] = []
    blob = " ".join(p for p in parts if p).lower()
    for key, vals in SYNONYM_HINTS.items():
        if key in blob:
            out.extend(vals)
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# 构造 UMR 记录
# --------------------------------------------------------------------------- #
def build_records() -> list[dict]:
    recs: list[dict] = []
    ts = BASE_TS_MS

    def add(category, name, brand=None, product_type=None, unit=None, cost=None,
            supplier=None, lead=None, attrs=None, tags=None, synonyms=None,
            identifiers=None, price_hist=None):
        uid = _ulid(ts + len(recs))
        recs.append({
            "material_uid": uid,
            "category": category,
            "product_type": product_type or category,
            "name": name,
            "brand": brand,
            "unit": unit,
            "unit_cost": cost,
            "currency": "AUD",
            "cost_basis": "ex_gst",
            "default_supplier": supplier,
            "lead_time_days": lead or 7,
            "attributes": attrs or {},
            "tags": tags or [],
            "synonyms": synonyms or [],
            "identifiers": identifiers or [],
            "prices": price_hist or [],
            "ref_count": random.choice([0, 0, 1, 2, 5, 9, 14, 23, 41]),
            "source_system": "joineryflow",
            "confidence": 1.0,
            "status": "active",
        })

    # --- board ------------------------------------------------------------ #
    for i, (brand, substrate, finish, colour, th, slen, swid, price, sup) in enumerate(BOARDS):
        sku = f"{th}-{colour[:4].upper()}-{substrate[:3].upper()}-{i:02d}"
        name = f"{th}mm {colour} {finish} {substrate}"
        add("board", name, brand=brand, product_type="Board" if finish != "Laminate" else "Laminate",
            unit="sheet", cost=price, supplier=sup, lead=5 if sup != "Briggs Veneers" else 21,
            attrs={"thickness_mm": th, "colour": colour, "substrate": substrate, "finish": finish,
                   "sheet_len_mm": slen, "sheet_wid_mm": swid,
                   "hmr": "Yes" if "MR" in substrate else "No",
                   "sided": "Double" if finish == "Melamine" else "Single"},
            tags=["carcass"] if th in (16, 18) else ["shelf"],
            synonyms=_hints(finish, substrate, colour) + [sku, f"{th}mm", colour],
            identifiers=[{"kind": "sku", "value": sku, "source_system": "joineryflow", "is_primary": True},
                         {"kind": "internal_ref", "value": f"BM-{200+i}", "source_system": "joineryflow"}],
            price_hist=[{"supplier": sup, "unit_cost": price, "valid_from": "2026-04-01"},
                        {"supplier": sup, "unit_cost": round(price * 0.94, 2), "valid_from": "2026-01-01"}])

    # --- hardware --------------------------------------------------------- #
    for i, (brand, name, htype, unit, price, sup) in enumerate(HARDWARE):
        sku = f"HW-{brand[:3].upper()}-{1000 + i}"
        add("hardware", name, brand=brand, product_type="Hardware", unit=unit, cost=price,
            supplier=sup, lead=7,
            attrs={"hardware_type": htype, "brand": brand},
            tags=["blum"] if brand == "Blum" else [],
            synonyms=_hints(htype, name) + [sku],
            identifiers=[{"kind": "sku", "value": sku, "source_system": "joineryflow", "is_primary": True}],
            price_hist=[{"supplier": sup, "unit_cost": price, "valid_from": "2026-06-01"}])

    # --- edging ----------------------------------------------------------- #
    for i, (brand, name, etype, colour, th, wid, roll, price, sup) in enumerate(EDGING):
        sku = f"ED-{th}-{wid}-{colour[:3].upper()}-{i:02d}"
        add("edging", name, brand=brand, product_type="Edging", unit="m", cost=price, supplier=sup,
            lead=10,
            attrs={"edge_type": etype, "colour": colour, "edge_thickness_mm": th,
                   "edge_width_mm": wid, "roll_length_m": roll, "glued": True},
            synonyms=_hints("edging", etype) + [sku, f"{wid}mm", etype],
            identifiers=[{"kind": "sku", "value": sku, "source_system": "joineryflow", "is_primary": True}])

    # --- glass ------------------------------------------------------------ #
    for i, (brand, name, gtype, th, colour, price) in enumerate(GLASS):
        sku = f"GL-{int(th*100):04d}-{colour[:3].upper()}-{i:02d}"
        add("glass", name, brand=brand, product_type="Glass", unit="m2", cost=price,
            supplier="Viridian Glass", lead=14,
            attrs={"glass_type": gtype, "thickness_mm": th, "colour": colour, "shaped": False},
            synonyms=_hints(gtype, "glass") + [sku, f"{th}mm"],
            identifiers=[{"kind": "sku", "value": sku, "source_system": "joineryflow", "is_primary": True}])

    # --- timber ----------------------------------------------------------- #
    for i, (brand, name, species, th, wid, ln) in enumerate(TIMBER):
        sku = f"TM-{species[:3].upper()}-{th}-{i:02d}"
        add("timber", name, brand=brand, product_type="Solid Timber", unit="m",
            cost=round(6.5 + th * 0.55 + wid * 0.04, 2), supplier=brand, lead=14,
            attrs={"species": species, "thickness_mm": th, "width_mm": wid, "length_mm": ln,
                   "finish": "DAR", "rating": "Select"},
            synonyms=_hints("timber", species) + [sku, species],
            identifiers=[{"kind": "sku", "value": sku, "source_system": "joineryflow", "is_primary": True}])

    # --- benchtop --------------------------------------------------------- #
    for i, (brand, name, mat, th, sup, price) in enumerate(BENCHTOP):
        sku = f"BT-{brand[:3].upper()}-{1000 + i}"
        add("benchtop", name, brand=brand, product_type="Laminate Benchtop" if mat == "Laminate" else "Stone",
            unit="slab", cost=price, supplier=sup, lead=21,
            attrs={"material": mat, "thickness_mm": th, "laminate_supplier": brand,
                   "bench_length_mm": 3000, "bench_width_mm": 1400, "shaped": False},
            tags=["stone"] if mat != "Laminate" else ["laminate"],
            synonyms=_hints(mat, "benchtop", brand) + [sku],
            identifiers=[{"kind": "sku", "value": sku, "source_system": "joineryflow", "is_primary": True},
                         {"kind": "slab_id", "value": f"hartwood-{brand[:3].upper()}-{2297+i}",
                          "source_system": "joineryflow"}])

    # --- appliance -------------------------------------------------------- #
    for i, (brand, name, sup, price) in enumerate(APPLIANCE):
        sku = f"AP-{brand[:3].upper()}-{1000 + i}"
        add("appliance", name, brand=brand, product_type="Appliance", unit="ea", cost=price,
            supplier=sup, lead=14,
            attrs={"manufacturer": brand},
            synonyms=[sku, brand],
            identifiers=[{"kind": "sku", "value": sku, "source_system": "joineryflow", "is_primary": True},
                         {"kind": "model_no", "value": f"hartwood-{brand[:3].upper()}-{7164+i}",
                          "source_system": "joineryflow"}])

    # --- custom ----------------------------------------------------------- #
    for i, (vendor, name, price, lead) in enumerate(CUSTOM):
        sku = f"CM-{1000 + i}"
        add("custom", name, brand=vendor, product_type="Contractor Manufacturing", unit="ea",
            cost=price, supplier=vendor, lead=lead,
            attrs={"vendor": vendor},
            synonyms=[sku],
            identifiers=[{"kind": "sku", "value": sku, "source_system": "joineryflow", "is_primary": True},
                         {"kind": "internal_ref", "value": f"hartwood-CM-{1000+i}",
                          "source_system": "joineryflow"}])

    # --- hire ------------------------------------------------------------- #
    for i, (sup, name, rate, unit) in enumerate(HIRE):
        sku = f"HR-{1000 + i}"
        add("hire", name, brand=sup, product_type="Equipment Hire", unit=unit, cost=rate,
            supplier=sup, lead=2,
            attrs={"rate_unit": unit, "rate": rate},
            synonyms=[sku],
            identifiers=[{"kind": "sku", "value": sku, "source_system": "joineryflow", "is_primary": True}])

    # --- consumable（joineryflow 里是 Supplies/Paint 页签，无独立表） ------- #
    for i, (brand, name, ctype, unit, price, sup) in enumerate(CONSUMABLE):
        sku = f"CS-{brand[:3].upper()}-{100 + i}"
        add("consumable", name, brand=brand,
            product_type="Paint" if ctype == "coating" else "Supplies",
            unit=unit, cost=price, supplier=sup, lead=3,
            attrs={"consumable_type": ctype},
            synonyms=_hints(ctype, brand) + [sku],
            identifiers=[{"kind": "sku", "value": sku, "source_system": "joineryflow", "is_primary": True}])

    # 少量归档样例，验证归档降权但不隐藏
    for r in random.sample([x for x in recs if x["category"] == "board"], 2):
        r["status"] = "archived"

    # 少量替代料关系
    by_name = {r["name"]: r for r in recs}
    subs = [("16mm White Melamine MDF", "18mm Grey Melamine MDF", "substitute"),
            ("Blumotion Soft Close Hinge 107° Clip Top", "Sensys Soft Close Hinge 110°", "substitute"),
            ("Caesarstone 6131 Bianco Drift 20mm", "Silestone Blanco City 20mm", "substitute"),
            ("Blumotion Soft Close Hinge 107° Clip Top", "Clip Top Hinge Plate 0mm Cruciform", "accessory")]
    for a, b, kind in subs:
        if a in by_name and b in by_name:
            by_name[a].setdefault("relations", []).append({"to_uid": by_name[b]["material_uid"], "kind": kind})

    return recs


# --------------------------------------------------------------------------- #
# 建库
# --------------------------------------------------------------------------- #
DDL = """
DROP TABLE IF EXISTS material;
DROP TABLE IF EXISTS material_identifier;
DROP TABLE IF EXISTS material_fts;

CREATE TABLE material (
    material_uid     TEXT PRIMARY KEY,
    category         TEXT NOT NULL,
    product_type     TEXT,
    name             TEXT NOT NULL,
    brand            TEXT,
    unit             TEXT,
    unit_cost        REAL,
    currency         TEXT DEFAULT 'AUD',
    default_supplier TEXT,
    lead_time_days   INTEGER,
    attributes       TEXT NOT NULL DEFAULT '{}',
    tags             TEXT NOT NULL DEFAULT '',
    synonyms         TEXT NOT NULL DEFAULT '',
    relations        TEXT NOT NULL DEFAULT '[]',
    prices           TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'active',
    ref_count        INTEGER NOT NULL DEFAULT 0,
    source_system    TEXT,
    confidence       REAL DEFAULT 1.0
);

CREATE TABLE material_identifier (
    material_uid TEXT NOT NULL,
    kind         TEXT NOT NULL,
    value        TEXT NOT NULL,
    source_system TEXT,
    is_primary   INTEGER DEFAULT 0,
    PRIMARY KEY (kind, value, source_system)
);

CREATE INDEX idx_mat_cat  ON material(category, status);
CREATE INDEX idx_mat_cost ON material(unit_cost);

-- FTS5：列名直接带权重，配合 bm25() 做加权排序
CREATE VIRTUAL TABLE material_fts USING fts5(
    material_uid UNINDEXED,
    ids      ,   -- A 级：全部编码
    name     ,   -- B 级：名称
    brand    ,   -- B 级：品牌
    dims     ,   -- C 级：供应商/类型/单位
    blob     ,   -- D 级：描述/属性/同义词/标签
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

# bm25 权重：ids > name/brand > dims > blob
BM25_WEIGHTS = (12.0, 8.0, 5.0, 2.0, 1.0)


def main() -> None:
    recs = build_records()

    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.executescript(DDL)

    for r in recs:
        ids = " ".join(i["value"] for i in r["identifiers"])
        dims = " ".join(x for x in [r["product_type"], r["category"], r["default_supplier"], r["unit"]] if x)
        blob = " ".join([
            " ".join(f"{k}:{v}" for k, v in r["attributes"].items()),
            " ".join(r["synonyms"]),
            " ".join(r["tags"]),
            r.get("description", ""),
        ])
        con.execute(
            "INSERT INTO material VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["material_uid"], r["category"], r["product_type"], r["name"], r["brand"],
             r["unit"], r["unit_cost"], r["currency"], r["default_supplier"], r["lead_time_days"],
             json.dumps(r["attributes"], ensure_ascii=False), ",".join(r["tags"]),
             json.dumps(r["synonyms"], ensure_ascii=False),
             json.dumps(r.get("relations", []), ensure_ascii=False),
             json.dumps(r["prices"], ensure_ascii=False),
             r["status"], r["ref_count"], r["source_system"], r["confidence"]),
        )
        con.execute(
            "INSERT INTO material_fts (material_uid, ids, name, brand, dims, blob) VALUES (?,?,?,?,?,?)",
            (r["material_uid"], ids, r["name"], r["brand"] or "", dims, blob),
        )
        for ident in r["identifiers"]:
            con.execute(
                "INSERT OR REPLACE INTO material_identifier VALUES (?,?,?,?,?)",
                (r["material_uid"], ident["kind"], ident["value"], ident.get("source_system"),
                 1 if ident.get("is_primary") else 0),
            )

    con.commit()
    con.execute("INSERT INTO material_fts(material_fts) VALUES('optimize')")
    con.commit()
    con.close()

    # 同时导出 UMR 交换格式（一行一条）
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    cats = {}
    for r in recs:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    print(f"OK  {len(recs)} 条材料 → {DB_PATH.name}")
    print("    " + "  ".join(f"{k}={v}" for k, v in sorted(cats.items())))
    print(f"    交换格式样例 → {JSONL_PATH.relative_to(HERE)}")


if __name__ == "__main__":
    main()
