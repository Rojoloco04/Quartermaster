"""MCP servers the assistant reaches its integrations through.

Each runs as ``qm mcp <name>`` over stdio, so the Discord bot (via the Agent
SDK) and ``claude`` in a terminal (via the vault's .mcp.json) share one
implementation and the same tokens. The server modules are thin on purpose:
validation, formatting and scope guarantees live in ``integrations``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import cache

from mcp.server.mcpserver.exceptions import ToolError

from ..config import Settings, load_settings

log = logging.getLogger(__name__)


@cache
def settings() -> Settings:
    return load_settings()


def run(service: str, fn: Callable[..., str], *args, **kwargs) -> str:
    """Call an integration function, turning any failure into a ToolError the
    model can read and act on (the SDK's own is a bare "Error executing tool")."""
    try:
        return fn(settings(), *args, **kwargs)
    except RuntimeError as exc:  # the integration's own error: already readable
        log.warning("%s.%s failed: %s", service, fn.__name__, exc)
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - HttpError, httpx, SpotifyException...
        log.exception("%s.%s failed", service, fn.__name__)
        # googleapiclient's HttpError carries a readable reason.
        raise ToolError(f"{service} request failed: {getattr(exc, 'reason', None) or exc}") from exc


def list_accounts(service: str, labels: list[str]) -> str:
    return ", ".join(labels) if labels else f"None yet. The owner runs: qm auth {service} <label>"
