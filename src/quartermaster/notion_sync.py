"""The daily Notion pull.

One-way: Notion to vault, never the reverse. The mirror is disposable — delete
``notion/`` and ``state.db`` and a full run rebuilds both. Nothing in the mirror
is authored here, which is why there is no conflict resolution anywhere in this
file.

Two things this is careful about:

- **Truncation is recorded, not hidden.** Notion truncates very large pages. A
  silently truncated mirror would make the agent confidently wrong about content
  it believes it has read, so truncation goes in the frontmatter.
- **Deletions are real.** A page unshared or trashed in Notion is removed from
  the mirror, or the agent keeps answering from content that no longer exists.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .config import Settings
from .integrations.notion import NotionClient, page_title

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

DQUOTE = '"'
SQUOTE = "'"


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "untitled"


@dataclass
class SyncStats:
    scanned: int = 0
    written: int = 0
    unchanged: int = 0
    removed: int = 0
    truncated: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        bits = [
            f"{self.scanned} pages scanned",
            f"{self.written} written",
            f"{self.unchanged} unchanged",
            f"{self.removed} removed",
        ]
        if self.truncated:
            bits.append(f"{len(self.truncated)} TRUNCATED")
        if self.failed:
            bits.append(f"{len(self.failed)} FAILED")
        return ", ".join(bits)


def _object_title(obj: dict) -> str:
    """Title of a page or a database. They store it differently."""
    if obj.get("object") == "database":
        parts = obj.get("title") or []
        text = "".join(p.get("plain_text", "") for p in parts).strip()
        return text or "Untitled database"
    return page_title(obj)


def _parent_id(obj: dict) -> str | None:
    parent = obj.get("parent") or {}
    for key in ("page_id", "database_id", "data_source_id", "block_id"):
        if parent.get(key):
            return str(parent[key]).replace("-", "")
    return None


def _vault_path(obj_id: str, index: dict[str, dict], notion_dir: Path) -> Path:
    """Mirror Notion's nesting as directories.

    Grep results are far more useful when the path says where a page lived.
    Cycles and unknown parents fall back to a shallower path rather than looping.
    """
    parts: list[str] = []
    seen: set[str] = set()
    current: str | None = obj_id

    while current and current in index and current not in seen:
        seen.add(current)
        parts.append(slugify(_object_title(index[current])))
        current = _parent_id(index[current])

    parts.reverse()
    # Short id suffix guarantees uniqueness when two siblings share a title.
    stem = f"{parts[-1] if parts else 'untitled'}-{obj_id[:8]}"
    return notion_dir.joinpath(*parts[:-1], f"{stem}.md")


def _frontmatter(obj: dict, path_hint: str, truncated: bool, unknown: list[str]) -> str:
    safe_title = _object_title(obj).replace(DQUOTE, SQUOTE)
    lines = [
        "---",
        f'notion_id: "{obj.get("id", "")}"',
        f'title: "{safe_title}"',
        f'url: "{obj.get("url", "")}"',
        f'last_edited: "{obj.get("last_edited_time", "")}"',
        f'notion_path: "{path_hint}"',
        "mirror: true",
    ]
    if truncated:
        lines += ["truncated: true", f"unknown_blocks: {len(unknown)}"]
    lines.append("---")

    if truncated:
        lines += [
            "",
            "> **This mirror is incomplete.** Notion truncated the page and "
            f"{len(unknown)} block(s) were not retrieved. Open the page in Notion "
            "before relying on it being complete.",
        ]
    return "\n".join(lines) + "\n\n"


def sync(settings: Settings, force: bool = False) -> SyncStats:
    settings.require("notion_token")
    stats = SyncStats()
    notion_dir = settings.notion_dir
    notion_dir.mkdir(parents=True, exist_ok=True)

    with NotionClient(settings.notion_token or "") as client, db.session(settings.db_path) as conn:
        index: dict[str, dict] = {}
        for obj in client.search_pages():
            index[str(obj["id"]).replace("-", "")] = obj

        stats.scanned = len(index)
        live_ids: set[str] = set()

        for obj_id, obj in index.items():
            live_ids.add(obj_id)
            path = _vault_path(obj_id, index, notion_dir)
            last_edited = obj.get("last_edited_time", "")

            row = conn.execute(
                "SELECT last_edited_at, vault_path FROM notion_pages WHERE page_id = ?",
                (obj_id,),
            ).fetchone()

            unchanged = (
                row is not None
                and row["last_edited_at"] == last_edited
                and Path(row["vault_path"]).exists()
            )
            if unchanged and not force:
                stats.unchanged += 1
                continue

            try:
                content = client.page_markdown(obj_id)
            except Exception as exc:  # noqa: BLE001 - one bad page must not end the sync
                stats.failed.append((_object_title(obj), str(exc)[:160]))
                continue

            rel = path.relative_to(notion_dir).as_posix()
            body = _frontmatter(obj, rel, content.truncated, content.unknown_block_ids)
            body += content.markdown

            # A renamed or moved page would otherwise leave its old file behind.
            if row and row["vault_path"] and Path(row["vault_path"]) != path:
                Path(row["vault_path"]).unlink(missing_ok=True)

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            stats.written += 1
            if content.truncated:
                stats.truncated.append(_object_title(obj))

            conn.execute(
                """
                INSERT INTO notion_pages
                    (page_id, vault_path, title, last_edited_at, content_hash, synced_at, archived)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(page_id) DO UPDATE SET
                    vault_path=excluded.vault_path, title=excluded.title,
                    last_edited_at=excluded.last_edited_at,
                    content_hash=excluded.content_hash,
                    synced_at=excluded.synced_at, archived=0
                """,
                (
                    obj_id,
                    str(path),
                    _object_title(obj),
                    last_edited,
                    hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
                    db.utcnow(),
                ),
            )

        stats.removed = _prune(conn, live_ids)
        _prune_empty_dirs(notion_dir)

    return stats


def _prune(conn: sqlite3.Connection, live_ids: set[str]) -> int:
    """Delete mirrored files for pages that are gone from Notion."""
    removed = 0
    rows = conn.execute(
        "SELECT page_id, vault_path FROM notion_pages WHERE archived = 0"
    ).fetchall()
    for row in rows:
        if row["page_id"] in live_ids:
            continue
        Path(row["vault_path"]).unlink(missing_ok=True)
        conn.execute("UPDATE notion_pages SET archived = 1 WHERE page_id = ?", (row["page_id"],))
        removed += 1
    return removed


def _prune_empty_dirs(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
