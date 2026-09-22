"""Notion REST client.

Notion is the source of truth for the user's knowledge, todos and wishlists.
This client only reads; changes are proposed in 90-System/pending.md and made
by the owner.

The important find here is ``GET /v1/pages/{id}/markdown``: a first-class REST
endpoint that returns a page as markdown. It means the mirror is a
markdown-to-markdown copy with no block-tree translation to get wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

# Notion's published guidance is an average of ~3 requests/second. A full
# workspace pull is the one job that will actually reach that, so throttle
# rather than rely on retry-after.
_MIN_INTERVAL = 1 / 3


@dataclass
class PageMarkdown:
    markdown: str
    truncated: bool
    unknown_block_ids: list[str]


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, token: str, timeout: float = 30.0):
        if not token:
            raise NotionError("No Notion token. Set NOTION_TOKEN in .env.")
        self._client = httpx.Client(
            base_url=API,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        self._last_call = 0.0

    def __enter__(self) -> "NotionClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        for attempt in range(5):
            self._throttle()
            resp = self._client.request(method, path, **kwargs)

            if resp.status_code == 429:
                # Notion tells us how long to wait; believe it rather than guess.
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise NotionError(f"{method} {path} -> {resp.status_code}: {resp.text[:400]}")
            return resp.json()

        raise NotionError(f"{method} {path} failed after retries")

    def search_pages(self) -> Iterator[dict]:
        """Every page the integration can see, oldest cursor first.

        Notion's search only returns content explicitly shared with the
        integration, so an empty result usually means nothing has been
        connected to it yet rather than an empty workspace.
        """
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "page_size": 100,
                "filter": {"value": "page", "property": "object"},
            }
            if cursor:
                body["start_cursor"] = cursor

            data = self._request("POST", "/search", json=body)
            yield from data.get("results", [])

            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    def retrieve_block_children(self, block_id: str) -> Iterator[dict]:
        """Every direct child block of a page or block, in order."""
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor

            data = self._request("GET", f"/blocks/{block_id}/children", params=params)
            yield from data.get("results", [])

            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    def retrieve_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")

    def create_page(self, parent_id: str, title: str, markdown: str) -> dict:
        return self._request("POST", "/pages", json={
            "parent": {"page_id": parent_id},
            "properties": {"title": {"title": [{"text": {"content": title[:200]}}]}},
            "markdown": markdown,
        })

    def append_markdown(self, page_id: str, markdown: str) -> None:
        self._request("PATCH", f"/pages/{page_id}/markdown", json={
            "type": "insert_content",
            "insert_content": {"content": markdown, "position": {"type": "end"}},
        })

    def page_markdown(self, page_id: str) -> PageMarkdown:
        data = self._request("GET", f"/pages/{page_id}/markdown")
        return PageMarkdown(
            markdown=extract_markdown(data),
            truncated=bool(data.get("truncated")),
            unknown_block_ids=list(data.get("unknown_block_ids") or []),
        )


# Keys that have carried page content, most current first. `markdown` is what
# the API returns today; `page_markdown` was a misreading of the docs, where it
# is actually the response's `object` type discriminator. Kept as a fallback in
# case the shape ever changes back.
_CONTENT_KEYS = ("markdown", "page_markdown", "content")

# Response fields that are metadata, so a string here is never page content.
_METADATA_KEYS = {"object", "id", "request_id", "url", "type"}


class MarkdownShapeError(NotionError):
    """The response carried content we did not know how to read.

    This is its own error because the failure it guards against is the quiet
    one: returning "" for a page that actually had text would mirror 79 pages
    of empty files and report success, leaving the agent certain it had read
    knowledge it never saw.
    """


def extract_markdown(data: dict) -> str:
    """Pull page content out of a markdown response.

    Returns "" only when the page is genuinely empty. If the payload holds
    substantial text under a key we do not recognise, this raises rather than
    silently returning nothing.
    """
    for key in _CONTENT_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
        # Tolerate a nested object shape without assuming one.
        if isinstance(value, dict):
            for inner in ("markdown", "content", "text"):
                nested = value.get(inner)
                if isinstance(nested, str) and nested.strip():
                    return nested

    # Nothing found. Before accepting "empty page", make sure we are not simply
    # looking in the wrong place.
    for key, value in data.items():
        if key in _METADATA_KEYS or key in _CONTENT_KEYS:
            continue
        if isinstance(value, str) and len(value.strip()) > 80:
            raise MarkdownShapeError(
                f"Response carries text under unexpected key {key!r} "
                f"({len(value)} chars). The API shape changed; update "
                f"_CONTENT_KEYS in integrations/notion.py rather than "
                f"mirroring empty pages."
            )

    return ""


def page_title(page: dict) -> str:
    """Best-effort title for a page object from search results."""
    props = page.get("properties") or {}
    for prop in props.values():
        if prop.get("type") == "title":
            parts = prop.get("title") or []
            text = "".join(p.get("plain_text", "") for p in parts).strip()
            if text:
                return text
    return "Untitled"
