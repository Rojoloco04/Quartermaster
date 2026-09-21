"""Clean up what Notion's markdown endpoint actually returns.

The endpoint emits a hybrid: real markdown, plus custom XML tags for the blocks
markdown cannot express. Left alone, a mirrored page contains things that are
actively harmful to an agent reading it:

- **Pre-signed S3 image URLs**, ~1,800 characters each, carrying temporary AWS
  credentials and ``X-Amz-Expires=3600``. They are dead an hour after the sync,
  they bury the actual prose, and they wreck grep output. The filename is the
  only part worth keeping.
- **``file://`` attachment blobs** — a URL-encoded JSON object where a filename
  should be.
- **``<empty-block/>``** — 208 of them across this vault, pure noise.
- **``<page>`` tags** for child pages. These are not noise: rewritten as
  relative markdown links they make the mirror *navigable*, so the agent can
  follow the hierarchy instead of guessing at filenames.

Everything here is lossy on purpose. The vault is a reading copy; Notion remains
the source of truth, and the page's ``url`` is always in the frontmatter.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

# <page url="https://app.notion.com/p/<id>">Title</page>
_PAGE_TAG = re.compile(r'<page\s+url="([^"]*)"\s*>(.*?)</page>', re.DOTALL)

# <file|pdf|video|audio|embed src="..." ...>  (self-closing or paired)
_MEDIA_TAG = re.compile(
    r'<(file|pdf|video|audio|embed|image)\s+src="([^"]*)"[^>]*?/?>(?:</\1>)?',
    re.IGNORECASE | re.DOTALL,
)

# <unknown url="..." alt="bookmark"/>
_UNKNOWN_TAG = re.compile(r'<unknown\s+([^>]*?)/?>', re.IGNORECASE)
_ATTR = re.compile(r'(\w[\w-]*)="([^"]*)"')

_EMPTY_BLOCK = re.compile(r"<empty-block\s*/?>", re.IGNORECASE)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)

# Layout wrappers: drop the tag, keep whatever is inside.
_LAYOUT = re.compile(
    r"</?(columns|column|colgroup|col|div|span)(\s[^>]*)?/?>", re.IGNORECASE
)

_TABLE = re.compile(r"<table([^>]*)>(.*?)</table>", re.IGNORECASE | re.DOTALL)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)

# A markdown image whose target is a signed, expiring URL.
_SIGNED_IMAGE = re.compile(r"!\[([^\]]*)\]\((https?://[^)]*X-Amz-[^)]*)\)")
_SIGNED_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\((https?://[^)]*X-Amz-[^)]*)\)")

_BLANKS = re.compile(r"\n{3,}")


def _attachment_name(src: str) -> str:
    """Pull a filename out of whatever Notion put in a src attribute.

    Three shapes occur: a ``file://`` JSON blob, a signed S3 URL, and an
    ordinary link. Only the filename is worth keeping from the first two.
    """
    if src.startswith("file://"):
        try:
            payload = json.loads(unquote(src[len("file://") :]))
            source = str(payload.get("source", ""))
            # "attachment:<uuid>:<filename>"
            return source.rsplit(":", 1)[-1] or "attachment"
        except (ValueError, TypeError):
            return "attachment"

    name = Path(unquote(urlparse(src).path)).name
    return name or "attachment"


def _is_signed(url: str) -> bool:
    return "X-Amz-" in url


def notion_id_from_url(url: str) -> str | None:
    """The 32-hex page id at the end of a Notion URL, if there is one."""
    match = re.search(r"([0-9a-f]{32})", url.replace("-", ""))
    return match.group(1) if match else None


def _convert_tables(text: str) -> str:
    def one_table(match: re.Match[str]) -> str:
        attrs, body = match.group(1), match.group(2)
        rows: list[list[str]] = []
        for row in _ROW.findall(body):
            cells = [clean_inline(c).replace("|", "\\|").strip() for c in _CELL.findall(row)]
            if cells:
                rows.append(cells)

        if not rows:
            return ""

        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]

        has_header = "header-row" in attrs and "true" in attrs
        if not has_header:
            # Markdown tables require a header row; an empty one keeps the
            # first data row from being silently promoted into it.
            rows.insert(0, [""] * width)

        out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n" + "\n".join(out) + "\n"

    return _TABLE.sub(one_table, text)


def clean_inline(text: str) -> str:
    """Tag cleanup that is safe to run inside a table cell."""
    text = _EMPTY_BLOCK.sub("", text)
    text = _BR.sub(" ", text)
    text = _LAYOUT.sub("", text)
    return text.strip()


def clean(
    markdown: str,
    *,
    link_targets: dict[str, str] | None = None,
) -> str:
    """Turn a raw endpoint response into something worth reading.

    ``link_targets`` maps a 32-hex Notion page id to a path relative to the page
    being written. Supplied ids become real markdown links; anything missing
    falls back to the Notion URL, so a page outside the mirror still resolves.
    """
    link_targets = link_targets or {}

    def page_link(match: re.Match[str]) -> str:
        url, title = match.group(1), clean_inline(match.group(2)) or "Untitled"
        page_id = notion_id_from_url(url)
        target = link_targets.get(page_id or "", url)
        return f"[{title}]({target})"

    text = _PAGE_TAG.sub(page_link, markdown)

    def media(match: re.Match[str]) -> str:
        kind, src = match.group(1).lower(), match.group(2)
        name = _attachment_name(src)
        if src.startswith("file://") or _is_signed(src):
            # The link would be dead within the hour; keep the name only.
            return f"[{kind}: {name}]"
        return f"[{kind}: {name}]({src})"

    text = _MEDIA_TAG.sub(media, text)

    def unknown(match: re.Match[str]) -> str:
        attrs = dict(_ATTR.findall(match.group(1)))
        label = attrs.get("alt") or "unsupported block"
        url = attrs.get("url")
        return f"[{label}]({url})" if url else f"[{label}]"

    text = _UNKNOWN_TAG.sub(unknown, text)

    text = _convert_tables(text)

    # Expiring media links. Done after tag handling so table cells are covered.
    text = _SIGNED_IMAGE.sub(
        lambda m: f"[image: {_attachment_name(m.group(2))}]", text
    )
    text = _SIGNED_LINK.sub(
        lambda m: f"[{m.group(1) or _attachment_name(m.group(2))}]", text
    )

    text = _EMPTY_BLOCK.sub("", text)
    text = _BR.sub("\n", text)
    text = _LAYOUT.sub("", text)

    text = _BLANKS.sub("\n\n", text)
    return text.strip() + "\n"
