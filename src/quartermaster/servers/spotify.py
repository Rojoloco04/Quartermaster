"""Spotify taste signal as MCP tools.

Thin on purpose: validation, formatting and the read-only guarantee live in
``integrations.spotify``. This file only maps tool calls onto it and turns
failures into messages the model can act on.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..config import Settings, load_settings
from ..integrations import spotify

server = MCPServer("spotify")
_settings: Settings | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def _run(fn: Callable[..., str], *args, **kwargs) -> str:
    try:
        return fn(_get_settings(), *args, **kwargs)
    except spotify.SpotifyError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - SpotifyException and friends
        raise ToolError(f"Spotify request failed: {exc}") from exc


@server.tool()
def list_accounts() -> str:
    """List the labels of the authorised Spotify accounts."""
    labels = spotify.accounts(_get_settings())
    return ", ".join(labels) if labels else "None yet. The owner runs: qm auth spotify <label>"


@server.tool()
def top_artists(account: str | None = None, time_range: str = "medium_term", limit: int = 10) -> str:
    """The owner's most-listened artists. time_range is short_term (~4 weeks),
    medium_term (~6 months) or long_term (years) - use this for "what kind of
    music do I like" rather than "what have I played recently"."""
    return _run(spotify.top_artists, account, time_range, limit)


@server.tool()
def top_tracks(account: str | None = None, time_range: str = "medium_term", limit: int = 10) -> str:
    """The owner's most-listened tracks. Same time_range options as top_artists."""
    return _run(spotify.top_tracks, account, time_range, limit)


@server.tool()
def saved_tracks(account: str | None = None, limit: int = 10) -> str:
    """Tracks the owner has explicitly saved to their library - a stronger
    taste signal than plays alone, since saving is a deliberate action."""
    return _run(spotify.saved_tracks, account, limit)


def main() -> None:
    server.run("stdio")
