#!/usr/bin/env python3
"""Validate every mappings/*.yaml against schema/mapping.schema.json.

    python scripts/check_mappings.py

The JSON Schema fixes the shape of a supplier mapping. A few rules span several
fields and therefore live here instead: template placeholders and family keys
must name a real field (or a default), field keys must be unique, and the file
must actually load through ingest.mapping.load_mapping. Prints one line per
mapping; on failure it lists every problem and exits 1.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_PATH = ROOT / "schema" / "mapping.schema.json"
MAPPING_DIR = ROOT / "mappings"
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value) -> list:
    return value if isinstance(value, list) else []


def problems(path: Path, validator: Draft202012Validator) -> list[str]:
    errors: list[str] = []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return [f"cannot read YAML: {exc}"]
    if not isinstance(data, dict):
        return ["top level must be a mapping object"]

    for err in validator.iter_errors(data):
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{where}: {err.message}")

    fields = [f for f in as_list(data.get("fields")) if isinstance(f, dict)]
    keys = [f["key"] for f in fields if isinstance(f.get("key"), str)]
    for key in sorted({k for k in keys if keys.count(k) > 1}):
        errors.append(f"duplicate field key {key!r}")

    known = set(keys) | set(as_dict(data.get("defaults")).keys())
    family = as_dict(data.get("family"))
    templates = (
        ("name_template", data.get("name_template")),
        ("family.name_template", family.get("name_template")),
    )
    for label, template in templates:
        if isinstance(template, str):
            for ref in PLACEHOLDER.findall(template):
                if ref not in known:
                    errors.append(f"{label} references unknown key {ref!r}")
    for ref in as_list(family.get("key")) + as_list(family.get("axis")):
        if isinstance(ref, str) and ref not in known:
            errors.append(f"family references unknown key {ref!r}")

    try:
        from ingest.mapping import load_mapping

        if not load_mapping(path).fields:
            errors.append("mapping defines no fields")
    except Exception as exc:
        errors.append(f"ingest.mapping cannot load this file: {exc}")

    return errors


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    paths = sorted(MAPPING_DIR.glob("*.yaml"))
    if not paths:
        print("FAIL: no mappings/*.yaml found")
        return 1

    failed = False
    for path in paths:
        found = problems(path, validator)
        if found:
            failed = True
            print(f"FAIL {path.relative_to(ROOT)}")
            for msg in found:
                print(f"  - {msg}")
        else:
            print(f"OK   {path.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
