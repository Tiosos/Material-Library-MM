#!/usr/bin/env python3
"""Material Library prototype server — zero dependencies (standard library + SQLite FTS5 only).

    python3 serve.py            # http://localhost:8848
    python3 serve.py 9000       # change the port

Endpoints
    GET /                      search UI
    GET /api/search?q=&category=&brand=&supplier=&limit=
    GET /api/material/{uid}    single record (incl. price history, substitutes, identifier book)
    GET /api/export?q=&format=jsonl|csv
    GET /api/suggest?q=        input completion
"""
from __future__ import annotations

import csv
import io
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import search as S

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8848


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # ---------------------------------------------------------------- utils
    def _send(self, body: bytes, ctype: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        self._send(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def _html(self, text: str):
        self._send(text.encode("utf-8"), "text/html; charset=utf-8")

    # ---------------------------------------------------------------- routes
    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}

        if u.path in ("/", "/index.html"):
            p = WEB / "index.html"
            if not p.exists():
                return self._send(b"index.html missing", "text/plain", 404)
            return self._html(p.read_text(encoding="utf-8"))

        if u.path == "/api/search":
            return self._json(S.search(
                qs.get("q", ""),
                category=qs.get("category", ""),
                brand=qs.get("brand", ""),
                supplier=qs.get("supplier", ""),
                limit=int(qs.get("limit", 60)),
            ))

        if u.path == "/api/suggest":
            r = S.search(qs.get("q", ""), limit=8)
            return self._json([{"name": x["name"], "category": x["category"]} for x in r["results"]])

        if u.path.startswith("/api/material/"):
            uid = u.path.rsplit("/", 1)[-1]
            d = S.get_one(uid)
            return self._json(d) if d else self._json({"error": "not found"}, 404)

        if u.path == "/api/export":
            r = S.search(qs.get("q", ""), limit=100000)
            fmt = qs.get("format", "jsonl")
            if fmt == "csv":
                buf = io.StringIO()
                keys = ["material_uid", "category", "product_type", "name", "brand",
                        "default_supplier", "unit", "unit_cost", "currency",
                        "lead_time_days", "status", "attributes", "synonyms"]
                w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
                w.writeheader()
                for x in r["results"]:
                    x = dict(x)
                    x["attributes"] = json.dumps(x.get("attributes", {}), ensure_ascii=False)
                    x["synonyms"] = "|".join(x.get("synonyms", []))
                    w.writerow(x)
                body = buf.getvalue().encode("utf-8-sig")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="materials.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            body = "".join(json.dumps({k: v for k, v in x.items() if not k.startswith("_")},
                                      ensure_ascii=False) + "\n"
                           for x in r["results"]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="materials.jsonl"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)

        return self._json({"error": "not found"}, 404)


def main():
    if not S.DB_PATH.exists():
        print("Database not found, run this first: python3 build_db.py")
        raise SystemExit(1)
    print(f"\n  Material Library prototype  →  http://localhost:{PORT}\n")
    print("  Try these queries (they exercise the four-level degradation):")
    print("    blum soft close hinge   full-text AND")
    print("    blu                     prefix (half-typed)")
    print("    blum hinge 450          relaxed (drops the terms that did not match)")
    print("    luminux                 fuzzy (typo → laminex)")
    print("    tempered glass          synonym → toughened")
    print("    countertop              alias lookup")
    print("    thickness:18 colour:black   attribute filter\n")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
