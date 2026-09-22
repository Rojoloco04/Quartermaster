"""The owner's Claude page in Notion as MCP tools. Scope is enforced in
``integrations.claude_page``: that page and its direct sub-pages only."""

from __future__ import annotations

from mcp.server import MCPServer

from . import run
from ..integrations import claude_page

server = MCPServer("notion")


@server.tool()
def read_claude_page(page_id: str | None = None) -> str:
    """Read the Claude page in Notion, or one of its sub-pages by id."""
    return run("notion", claude_page.read, page_id)


@server.tool()
def append_to_claude_page(markdown: str, page_id: str | None = None) -> str:
    """Append markdown to the end of the Claude page (or one of its sub-pages).
    This is the owner's page for anything worth keeping in Notion: write freely."""
    return run("notion", claude_page.append, markdown, page_id)


@server.tool()
def create_claude_subpage(title: str, markdown: str) -> str:
    """Create a new page under the Claude page, for anything long enough to
    deserve its own page. The rest of Notion is not writable: propose changes
    there in 90-System/pending.md instead."""
    return run("notion", claude_page.create, title, markdown)
