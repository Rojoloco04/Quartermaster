"""Spotify taste signal as MCP tools."""

from __future__ import annotations

from mcp.server import MCPServer

from . import list_accounts as _accounts, run, settings
from ..integrations import spotify

server = MCPServer("spotify")


@server.tool()
def list_accounts() -> str:
    """List the labels of the authorised Spotify accounts."""
    return _accounts("spotify", spotify.accounts(settings()))


@server.tool()
def top_artists(account: str | None = None, time_range: str = "medium_term", limit: int = 10) -> str:
    """The owner's most-listened artists. time_range is short_term (~4 weeks),
    medium_term (~6 months) or long_term (years) - use this for "what kind of
    music do I like" rather than "what have I played recently"."""
    return run("spotify", spotify.top_artists, account, time_range, limit)


@server.tool()
def top_tracks(account: str | None = None, time_range: str = "medium_term", limit: int = 10) -> str:
    """The owner's most-listened tracks. Same time_range options as top_artists."""
    return run("spotify", spotify.top_tracks, account, time_range, limit)


@server.tool()
def saved_tracks(account: str | None = None, limit: int = 10) -> str:
    """Tracks the owner has explicitly saved to their library - a stronger
    taste signal than plays alone, since saving is a deliberate action."""
    return run("spotify", spotify.saved_tracks, account, limit)

