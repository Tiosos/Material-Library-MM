"""Map columns from a supplier's free-form headers.

Design goal: every supplier's headers are different (`Part no.` / `Artikelnummer` / `Item no.` ...),
so instead of asking them to change their format, we give each supplier a `mappings/<vendor>.yaml`:

    fields:
      - key: part_no            # short name used in the template
        target: identifier      # identifier | attribute | measure | column
        kind: supplier_code
        aliases: ["Part no.", "Article no.", "Artikelnummer"]
        required: true

Header match order: normalized exact → contains → difflib close match (≥0.82). Columns that
match nothing can be downgraded wholesale to attributes (`extra_headers: attributes`), so a
supplier adding a column will not break the import.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

import yaml

RESERVED_TARGETS = {"identifier", "attribute", "measure", "column"}
_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def norm(s: str) -> str:
    """Normalize a header: flatten case, whitespace, punctuation, and BOM."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lstrip("\ufeff").lower())


def to_number(value: Any) -> float | None:
    """'110°' / '1,8 mm' / '$42.00' → number. Takes the first number, tolerating thousands
    separators and decimal commas."""
    if value is None or value == "":
        return None
    s = str(value).replace("\u00a0", " ")
    m = _NUM_RE.search(s)
    if not m:
        return None
    raw = m.group(0)
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def to_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "ja", "x"}:
        return True
    if s in {"0", "false", "no", "n", "nein"}:
        return False
    return None


TRANSFORMS = {
    "string": lambda v: None if v is None else str(v).strip(),
    "number": to_number,
    "bool": to_bool,
    "upper": lambda v: None if v is None else str(v).strip().upper(),
    "lower": lambda v: None if v is None else str(v).strip().lower(),
}


class Mapping:
    def __init__(self, data: dict, path: Path | None = None):
        self.data = data
        self.path = path
        self.vendor: str = data.get("vendor", "unknown")
        self.source_system: str = data.get("source_system", self.vendor)
        self.defaults: dict = data.get("defaults") or {}
        self.fields: list[dict] = data.get("fields") or []
        self.columns: dict[str, dict] = {}     # field key -> spec
        self.alias_index: dict[str, str] = {}  # normalized alias -> field key
        for f in self.fields:
            if "key" not in f or "target" not in f:
                raise ValueError(f"mapping field needs key+target: {f}")
            if f["target"] not in RESERVED_TARGETS:
                raise ValueError(f"unknown target {f['target']!r} in field {f['key']}")
            self.columns[f["key"]] = f
            for alias in [f["key"], *f.get("aliases", [])]:
                self.alias_index[norm(alias)] = f["key"]

    # ---- Header resolution ----------------------------------------------
    def resolve(self, headers: list[str]) -> tuple[dict[str, str], list[str]]:
        """Return ({field_key: header}, [unmatched headers])."""
        resolved: dict[str, str] = {}
        unmatched: list[str] = []
        for h in headers:
            n = norm(h)
            key = self.alias_index.get(n)
            if key is None:
                # containment: 'unitprice($)' matches the alias 'unit price'
                for alias, k in self.alias_index.items():
                    if len(alias) >= 4 and (alias in n or n in alias):
                        key = k
                        break
            if key is None:
                close = difflib.get_close_matches(n, list(self.alias_index), n=1, cutoff=0.82)
                if close:
                    key = self.alias_index[close[0]]
            if key and key not in resolved:
                resolved[key] = h
            elif key:
                unmatched.append(h)      # second column for the same field; treat as an extra column
            else:
                unmatched.append(h)
        return resolved, unmatched

    # ---- Cell value extraction ------------------------------------------
    def value(self, row: dict, header: str | None, spec: dict) -> Any:
        if not header:
            return spec.get("default")
        raw = row.get(header)
        fn = TRANSFORMS.get(spec.get("transform", "string"), TRANSFORMS["string"])
        return fn(raw)


def load_mapping(path: str | Path) -> Mapping:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Mapping(data, p)
