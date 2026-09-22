"""The one place in Notion the agent may write: the owner's "Claude" page.

Everything else in Notion is the owner's, and changes there are proposals
(``notion_writes``) applied only after a Confirm in Discord. The page id comes from ``notion.claude_page_id`` in
config.toml, and every write is checked here to be that page or a page
directly beneath it. The check is in code, not in a prompt: the tool cannot be
talked into writing anywhere else.
"""

from __future__ import annotations

from ..config import Settings
from .notion import NotionClient, NotionError, page_title


def _norm(page_id: str) -> str:
    return page_id.replace("-", "").strip().lower()


def _root(settings: Settings) -> str:
    root = _norm(settings.prefs["notion"].get("claude_page_id") or "")
    if not root:
        raise NotionError("No Claude page configured. Set notion.claude_page_id in config.toml.")
    settings.require("notion_token")
    return root


def _target(client: NotionClient, root: str, page_id: str | None) -> str:
    """The page to write: the Claude page itself, or one of its direct children."""
    if not page_id or _norm(page_id) == root:
        return root
    parent = (client.retrieve_page(page_id).get("parent") or {}).get("page_id") or ""
    if _norm(parent) != root:
        raise NotionError("That page isn't under the Claude page. Only the Claude page and its sub-pages are writable.")
    return _norm(page_id)


def read(settings: Settings, page_id: str | None = None) -> str:
    root = _root(settings)
    with NotionClient(settings.notion_token or "") as client:
        target = _target(client, root, page_id)
        return client.page_markdown(target).markdown or "(empty page)"


def append(settings: Settings, markdown: str, page_id: str | None = None) -> str:
    root = _root(settings)
    with NotionClient(settings.notion_token or "") as client:
        target = _target(client, root, page_id)
        client.append_markdown(target, markdown)
    return f"Appended to page {target}."


def create(settings: Settings, title: str, markdown: str) -> str:
    root = _root(settings)
    with NotionClient(settings.notion_token or "") as client:
        page = client.create_page(root, title, markdown)
    return f"Created '{page_title(page) or title}' under the Claude page: {page.get('url', '')}"
