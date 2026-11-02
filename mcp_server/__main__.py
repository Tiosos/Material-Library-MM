"""Entry point: `python -m mcp_server` (docs/mcp-server.md 6).

stdio is the default because it is the only transport a desktop host can start without
configuration; `--transport http` serves the same tools over streamable HTTP.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import ConfigError, Settings, apply_overrides, from_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mcp_server",
        description="Read-only MCP server for the Material Library catalogue.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=None,
        help="stdio for a local host (default), http for a shared server",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="base URL of the read API; defaults to ML_API_BASE_URL or http://127.0.0.1:8848",
    )
    parser.add_argument("--version", action="version", version=f"mcp_server {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings: Settings = apply_overrides(
            from_env(), transport=args.transport, base_url=args.base_url
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    from .server import build_server

    server = build_server(settings)
    if settings.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=settings.http_host, port=settings.http_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
