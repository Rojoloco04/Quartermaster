"""Notion REST client.

Notion is the source of truth for the user's knowledge, todos and wishlists. This
client only reads (plus one narrow write path for the `claude` page); every
other change goes through 90-System/pending.md for approval.

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

    def page_markdown(self, page_id: str) -> PageMarkdown:
        data = self._request("GET", f"/pages/{page_id}/markdown")
        raw = data.get("page_markdown")
        # The field is documented as an object; older responses returned a bare
        # string. Accept both rather than lose the page over a shape change.
        if isinstance(raw, dict):
            text = raw.get("markdown") or raw.get("content") or ""
        else:
            text = raw or ""
        return PageMarkdown(
            markdown=text,
            truncated=bool(data.get("truncated")),
            unknown_block_ids=list(data.get("unknown_block_ids") or []),
        )

    def replace_page_markdown(self, page_id: str, markdown: str) -> None:
        """Overwrite a page's content.

        Only ever called for the `claude` page. Every other write is a proposal
        in 90-System/pending.md until the user approves it.
        """
        self._request(
            "PATCH",
            f"/pages/{page_id}/markdown",
            json={"replace_content": markdown},
        )


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
