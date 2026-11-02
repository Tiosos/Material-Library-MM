"""HTTP client for the read API (docs/mcp-server.md 7).

The only place in `mcp_server/` that knows the API's address, token and header names. It does one
thing: turn a route plus query parameters into a decoded JSON body, or into an `ApiError` carrying
the API's own `code` and `message` (docs/mcp-server.md 8). No retries, no caching, no SQL.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from .config import Settings

# A tool call is a conversation turn, so a stuck request must end before the model gives up.
DEFAULT_TIMEOUT = 10.0


class ApiError(Exception):
    """An API failure, in the API's own vocabulary.

    `code` and `message` come from the response body unchanged; `status` is the HTTP status, or
    `None` when the request never reached the API. `detail` is the API's optional detail object.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.detail = dict(detail) if detail else None

    def to_tool_error(self) -> ToolError:
        """Render for the model: code, sentence, and detail when the API sent one."""
        text = f"{self.code}: {self.message}"
        if self.detail:
            text = f"{text} ({json.dumps(self.detail, sort_keys=True, ensure_ascii=False)})"
        return ToolError(text)


def _encode(value: Any) -> str:
    """Query strings carry JSON for objects; encode once, deterministically."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def clean_params(params: Mapping[str, Any] | None) -> dict[str, str]:
    """Drop unset values and stringify the rest.

    Dicts and lists become compact JSON, which is what the API's `attrs` and `measures` parameters
    expect (`docs/api-v1.md` 3.1); everything else is passed as text and validated by the API.
    """

    out: dict[str, str] = {}
    for key, value in (params or {}).items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            out[key] = "1" if value else "0"
        elif isinstance(value, (dict, list)):
            if value == {} or value == []:
                continue
            out[key] = _encode(value)
        else:
            out[key] = str(value)
    return out


class ApiClient:
    """Thin wrapper over `httpx2.Client` with the library's headers and error mapping."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx2.BaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        headers = {"X-Actor": settings.actor, "Accept": "application/json"}
        if settings.token:
            headers["Authorization"] = f"Bearer {settings.token}"
        self.base_url = settings.base_url
        self._client = httpx2.Client(
            base_url=settings.base_url,
            headers=headers,
            timeout=timeout if timeout is not None else (settings.timeout or DEFAULT_TIMEOUT),
            transport=transport,
            follow_redirects=False,
        )

    # -- lifecycle -------------------------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- requests --------------------------------------------------------------------------
    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET one route; return the decoded body or raise `ApiError`."""

        try:
            response = self._client.get(path, params=clean_params(params))
        except httpx2.Timeout as exc:
            raise ApiError(
                "unavailable",
                f"the API at {self.base_url} did not answer in time ({type(exc).__name__})",
            ) from None
        except httpx2.HTTPError as exc:
            raise ApiError(
                "unavailable",
                f"the API at {self.base_url} is not reachable ({type(exc).__name__})",
            ) from None

        try:
            body = response.json()
        except ValueError:
            raise ApiError(
                "bad_response",
                f"the API answered HTTP {response.status_code} with a body that is not JSON",
                status=response.status_code,
            ) from None

        if response.is_success:
            return body

        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and error.get("code"):
            raise ApiError(
                str(error["code"]),
                str(error.get("message") or "the API refused the request"),
                status=response.status_code,
                detail=error.get("detail"),
            )
        raise ApiError(
            "error",
            f"the API answered HTTP {response.status_code} without an error object",
            status=response.status_code,
        )
