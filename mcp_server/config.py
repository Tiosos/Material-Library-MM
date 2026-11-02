"""Configuration for the MCP server (docs/mcp-server.md 6).

Read once at startup. Every value is an environment variable with a default that works on a
developer machine: no config file, no credential store, and no database DSN - this process talks
to the HTTP API and nothing else (docs/mcp-server.md 3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

TRANSPORTS = ("stdio", "http")
DEFAULT_BASE_URL = "http://127.0.0.1:8848"
MAX_ACTOR = 64


class ConfigError(RuntimeError):
    """Raised when the process is misconfigured; the process should not start."""


@dataclass(frozen=True)
class Settings:
    """The six knobs in docs/mcp-server.md 6, validated once."""

    base_url: str = DEFAULT_BASE_URL
    token: str = ""
    actor: str = "mcp"
    timeout: float = 10.0
    transport: str = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = 8849


def _text(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _positive_float(name: str, default: str, ceiling: float = 300.0) -> float:
    raw = _text(name, default)
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number of seconds, got {raw!r}") from None
    if not 0.1 <= value <= ceiling:
        raise ConfigError(f"{name} must be between 0.1 and {ceiling:g} seconds, got {value:g}")
    return value


def _port(name: str, default: str) -> int:
    raw = _text(name, default)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a port number, got {raw!r}") from None
    if not 1 <= value <= 65535:
        raise ConfigError(f"{name} must be between 1 and 65535, got {value}")
    return value


def from_env() -> Settings:
    """Build settings from the environment, rejecting values that cannot work."""

    base_url = _text("ML_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"ML_API_BASE_URL must start with http:// or https://, got {base_url!r}")

    actor = _text("MCP_ACTOR", "mcp") or "mcp"
    if len(actor) > MAX_ACTOR:
        raise ConfigError(f"MCP_ACTOR must be at most {MAX_ACTOR} characters, got {len(actor)}")

    transport = (_text("MCP_TRANSPORT", "stdio") or "stdio").lower()
    if transport not in TRANSPORTS:
        raise ConfigError(f"MCP_TRANSPORT must be one of {', '.join(TRANSPORTS)}, got {transport!r}")

    return Settings(
        base_url=base_url,
        token=_text("ML_API_TOKEN", ""),
        actor=actor,
        timeout=_positive_float("MCP_TIMEOUT", "10"),
        transport=transport,
        http_host=_text("MCP_HTTP_HOST", "127.0.0.1") or "127.0.0.1",
        http_port=_port("MCP_HTTP_PORT", "8849"),
    )


def apply_overrides(settings: Settings, *, transport: str | None = None, base_url: str | None = None) -> Settings:
    """Command-line flags win over the environment, and are validated the same way."""

    changes: dict[str, object] = {}
    if transport is not None:
        value = transport.lower()
        if value not in TRANSPORTS:
            raise ConfigError(f"--transport must be one of {', '.join(TRANSPORTS)}, got {transport!r}")
        changes["transport"] = value
    if base_url is not None:
        value = base_url.rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ConfigError(f"--base-url must start with http:// or https://, got {base_url!r}")
        changes["base_url"] = value
    return replace(settings, **changes) if changes else settings
