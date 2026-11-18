#!/usr/bin/env python3
"""Blum TANDEMBOX antaro catalogue -> product CSV extractor.

Why a separate file: the drawer-section "Order information" layout differs from the
three-column hinge section handled by pdf_blum.py. This one reads the numbered
component rows (1 Cabinet profiles, 2 Drawer side set, 3 Chipboard back fixings,
4 Front fixing bracket, 5 Front piece, 6a Handle, 7 Cross gallery) and the accessory
tables (Steel back, screws, centres, ...), pairing each part number with the
nearest nominal-length / diameter value on its row.

Output columns are chosen to line up with mappings/antaro.yaml and the generic
importer. The catalogue has no prices and no supply dates: this is a catalogue-only
extract and is imported with --no-price; prices come from a separate price list.

No price/date is invented. Rows whose component or drawer type cannot be resolved
are still emitted (part number is the key); the quality report surfaces gaps.

Usage:
    python -m ingest.pdf_antaro "Antaro Drawers.pdf" -o ingest/samples/antaro_products.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import pymupdf

# Part numbers in this catalogue. Covers drawer sides 378N4002SA, profiles
# 578.4001M, fixings Z30N000S.04, galleries ZRG.207RSIC, handles ZIF.80M5,
# screws 609.1500 / 661.1000.HG / M01.ZZ03.01. Excludes web codes (DQC..),
# colour codes (R9006) and placeholder codes containing "xxx".
PART_RE = re.compile(
    r"^(?:\d{3}[A-Z]\d{3,4}[A-Z]{0,2}"          # 378N4002SA
    r"|\d{3}\.\d{4}[A-Z]?"                      # 578.4001M, 609.1500
    r"|\d{3}\.\d{4}\.[A-Z]{2}"                  # 661.1000.HG
    r"|[A-Z]{2,3}\.?\d{2,4}[A-Z0-9]{0,4}"       # ZRG.1046Z, ZIF.80M5, ZRF.2210
    r"|[A-Z]\d{2}[A-Z]\d{3,4}[A-Z]{0,2}(?:\.\d{1,2})?"  # Z30N000S.04, Z31L1036A
    r"|[A-Z]\d{2}\.[A-Z0-9]{2,5}(?:\.[A-Z0-9]{2})?"      # M01.ZZ03.01, M01.ZZB3
    r")$"
)
NOISE_RE = re.compile(r"^(?:DQC\w*|R\d{4}|2-dimensional|3-dimensional)$")
# A numeric spec is a nominal length (270..650) or a screw diameter/length (2.7..20.0).
SPEC_RE = re.compile(r"^(?:2[0-9]{2}|[3-6][0-9]{2}|\d{1,2}\.\d)(?:\d)?$")
# A component number is a small standalone token in the left margin (x < 100),
# e.g. "1", "2a", "6a". Its title words sit to its right on the same visual row.
COMPONENT_NUM_RE = re.compile(r"^\d{1,2}[a-z]?$")
DRAWER_RE = re.compile(
    r"Drawer\s*[–-]\s*([NMK])\b|Inner drawer\s*[–-]\s*([NMK])\b|"
    r"High fronted pull-out[^\n]*?\b([C])\b|Inner pull-out[^\n]*?\b([CK])\b",
    re.IGNORECASE,
)
FIELDS = ["Part no.", "Component", "Component code", "Drawer type",
          "Specification", "Colour", "Page", "System"]


def is_part(token: str) -> bool:
    if not PART_RE.match(token) or NOISE_RE.match(token):
        return False
    if "xxx" in token.lower():
        return False
    # Numeric-only forms are valid too (e.g. the 609.1500 chipboard screw).
    return True


def _spec_on_row(words: list, part_word: tuple) -> str:
    """Nearest numeric spec to the left of the part on the same visual row."""
    x, y = part_word[0], part_word[1]
    specs = [w[4] for w in words
             if abs(w[1] - y) < 4.0 and w[0] < x - 3 and SPEC_RE.match(w[4])]
    return specs[-1] if specs else ""


def _component_at(words: list, part_word: tuple) -> tuple[str, str]:
    """Nearest numbered component label above the part.

    Component numbers ("1", "2a", ...) are standalone tokens in the left margin;
    their title words sit to the right on the same row. Return (code, title).
    """
    x, y = part_word[0], part_word[1]
    numbers = [w for w in words
               if w[0] < 100 and COMPONENT_NUM_RE.match(w[4])
               and w[1] < y - 2 and w[1] > y - 130]
    if not numbers:
        return "", ""  # accessory table: no numbered component row
    label = max(numbers, key=lambda w: w[1])  # closest above
    ly = label[1]
    title_words = sorted((w for w in words
                          if abs(w[1] - ly) < 3 and w[0] > label[0] + 8
                          and w[0] < 300 and re.match(r"^[A-Za-z]", w[4])),
                         key=lambda w: w[0])
    title = " ".join(w[4] for w in title_words[:4]).strip()
    return label[4], title


def _drawer_type(text: str) -> str:
    m = DRAWER_RE.search(text)
    if m:
        groups = [g for g in m.groups() if g]
        kind = "Inner" if "inner" in m.group(0).lower() else (
            "High fronted" if "high fronted" in m.group(0).lower() else "Standard")
        return f"{kind} {groups[0].upper()}".strip()
    return ""


def _component_name(text: str, code: str) -> str:
    names = {
        "1": "Cabinet profiles", "2": "Drawer side set", "2a": "Drawer sides",
        "2b": "Cover caps", "3": "Chipboard back fixings", "4": "Front fixing bracket",
        "5": "Front piece", "6": "Handle", "6a": "Handle", "7": "Cross gallery",
    }
    if code in names:
        return names[code]
    for known in ("Steel back", "Front/base stabiliser", "Side stabilisation",
                  "Flexible gallery fixing", "Chipboard back fixing", "Screws",
                  "Centre bit", "Door buffer", "Screwdriver", "Gallery"):
        if known.lower() in text.lower():
            return known
    return ""


def _colour(text: str) -> str:
    found = []
    if "silk white" in text.lower() or re.search(r"\bSW\b", text):
        found.append("SW")
    if "R9006" in text or "RAL 9006" in text:
        found.append("R9006")
    if "WGR" in text or "white grey" in text.lower():
        found.append("WGR")
    return "|".join(found)


def extract_page(page, seen: set[str] | None = None) -> list[dict]:
    text = page.get_text()
    if "Order information" not in text:
        return []
    words = page.get_text("words")
    dtype = _drawer_type(text)
    colour = _colour(text)
    system = "TANDEMBOX antaro"
    if seen is None:
        seen = set()
    rows = []
    for w in words:
        part = w[4]
        if not is_part(part) or part in seen:
            continue
        seen.add(part)
        code, comp_title = _component_at(words, w)
        rows.append({
            "Part no.": part,
            "Component": comp_title or _component_name(text, code),
            "Component code": code,
            "Drawer type": dtype,
            "Specification": _spec_on_row(words, w),
            "Colour": colour,
            "Page": page.number + 1,
            "System": system,
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TANDEMBOX antaro catalogue -> product CSV")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", default="ingest/samples/antaro_products.csv")
    args = ap.parse_args(argv)

    doc = pymupdf.open(args.pdf)
    rows: list[dict] = []
    seen: set[str] = set()  # global: a part appears on many pages; keep its first sighting
    for page in doc:
        rows.extend(extract_page(page, seen))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with_spec = sum(1 for r in rows if r["Specification"])
    print(f"{len(rows)} parts ({with_spec} with a spec) -> {out}")
    for r in rows[:8]:
        print("  ", r["Part no."], "|", r["Component"], "|",
              r["Drawer type"], "|", r["Specification"], "| p", r["Page"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
