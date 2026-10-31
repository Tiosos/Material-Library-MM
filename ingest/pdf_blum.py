#!/usr/bin/env python3
"""Blum catalogue (Catalogue and technical manual) → CSV extractor.

Why a separate file: the catalogue layout is vendor-private, and the extraction rules are
effectively a one-off "column mapping". The generic import pipeline only understands tabular
data (CSV/XLSX), so PDF extraction happens here and the result is then handed to the pipeline.

Layout (2024/2025 edition, verified):
    ┌ three columns: Overlay application / Dual application / Inset application
    │ each column: Boss (INSERTA / Screw-on / Knock-in, merged vertically)
    │              Hinge (CLIP top / BLUMOTION)
    │              Spring (● with spring / ○ unsprung)
    │              Colour (NI / ONS)
    │              Part no. (71B3590 / 70T3590.TL)
    └ page header: "Hinge systems 〉 CLIP top BLUMOTION | CLIP top – hinges 〉 Standard application"
       and "110° hinge"

Usage:
    python -m ingest.pdf_blum "F:/.../Blum Catalogue.pdf" --pages 80-110 -o ingest/samples/blum_hinges.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import pymupdf

PART_RE = re.compile(r"^\d{2}[A-Z]\d{3,4}(\.[A-Z0-9]{1,4})?$")
BOSS_WORDS = {"INSERTA", "Screw-on", "Knock-in", "Expando", "Press-in"}
COLUMN_NAMES = [("Overlay", 0, 200), ("Dual", 200, 380), ("Inset", 380, 700)]


def _column_of(x: float) -> str:
    for name, lo, hi in COLUMN_NAMES:
        if lo <= x < hi:
            return name
    return "Overlay"


def _page_header(text: str) -> tuple[str, str]:
    """Return (system name, angle)."""
    system = ""
    m = re.search(r"Hinge systems\s*[〉>]\s*([^|〈\n]+)", text)
    if m:
        system = m.group(1).strip()
    angle = ""
    m = re.search(r"(\d{2,3}°)\s+hinge", text)
    if m:
        angle = m.group(1)
    return system, angle


def extract_page(page) -> list[dict]:
    text = page.get_text()
    if "Order information" not in text or "Part" not in text:
        return []
    system, angle = _page_header(text)
    words = page.get_text("words")  # x0, y0, x1, y1, word
    rows: list[dict] = []
    for w in words:
        if not PART_RE.match(w[4]):
            continue
        x, y, part = w[0], w[1], w[4]
        col = _column_of(x)
        lo, hi = next((lo, hi) for name, lo, hi in COLUMN_NAMES if name == col)

        # same column, near the same row
        near = [q for q in words if lo <= q[0] < hi and abs(q[1] - y) < 4.0]
        above = [q for q in words if lo <= q[0] < hi and q[1] < y - 2]

        spring = next((q[4] for q in near if q[4] in ("●", "○")), "")
        colour = "NI"
        if any(q[4] == "ONS" for q in near):
            colour = "NI/ONS"
        boss = ""
        for q in sorted(above, key=lambda q: -q[1]):
            if q[4] in BOSS_WORDS:
                boss = q[4]
                break
        hinge = "CLIP top"
        # "BLUMOTION" may sit above the part number or one line below; decide by ±10pt
        if any(q[4] == "BLUMOTION" and abs(q[1] - y) < 10 for q in words
               if lo <= q[0] < hi):
            hinge = "CLIP top BLUMOTION"

        rows.append({
            "Part no.": part,
            "Hinge": hinge,
            "Boss": boss,
            "Spring": "with spring" if spring == "●" else ("unsprung" if spring == "○" else ""),
            "Colour": colour,
            "Application": col,
            "System": system,
            "Opening angle": angle,
            "Page": page.number + 1,
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Blum catalogue → CSV")
    ap.add_argument("pdf")
    ap.add_argument("--pages", default="80-110", help="page range, e.g. 80-110")
    ap.add_argument("-o", "--out", default="ingest/samples/blum_hinges.csv")
    args = ap.parse_args(argv)

    lo, _, hi = args.pages.partition("-")
    first, last = int(lo), int(hi or lo)

    doc = pymupdf.open(args.pdf)
    rows: list[dict] = []
    for i in range(first - 1, min(last, doc.page_count)):
        rows.extend(extract_page(doc[i]))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["Part no.", "Hinge", "Boss", "Spring", "Colour", "Application",
              "System", "Opening angle", "Page"]
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"pages {first}-{last}: {len(rows)} rows -> {out}")
    for r in rows[:5]:
        print("  ", r["Part no."], "|", r["Hinge"], "|", r["Boss"], "|", r["Colour"], "|", r["Opening angle"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
