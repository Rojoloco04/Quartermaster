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
from .notion_clean import clean
from .integrations.notion import NotionClient, page_title

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

# Characters Windows forbids in a filename, plus control characters.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")

# Device names Windows still reserves, with or without an extension.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

DQUOTE = '"'
SQUOTE = "'"


def _has_letters_or_digits(text: str) -> bool:
    """True if the text contains a letter or digit in any script.

    Distinguishes a title ASCII-folding cannot represent (中文) from one that
    is genuinely just punctuation ("!!!"). The first is worth preserving
    verbatim; the second should become "untitled".
    """
    return any(unicodedata.category(ch)[0] in ("L", "N") for ch in text)


def slugify(text: str, max_len: int = 60) -> str:
    """Turn a page title into a filename component.

    ASCII is preferred because it greps and types easily. But ASCII-folding a
    title that has no ASCII in it at all — 中文, 日本語, 한국어 — yields an empty
    string, and three such pages previously collapsed onto one filename and
    silently overwrote each other. So when folding destroys the title, keep the
    original characters instead. NTFS and git both handle them fine, and
    `中文-<id>.md` is a far more useful name than `untitled-<id>.md`.
    """
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = _SLUG_STRIP.sub("-", folded.lower()).strip("-")

    if not slug and _has_letters_or_digits(text):
        # Folding destroyed a real title. Preserve it, minus what the
        # filesystem forbids. Guarded by the letter check so a title of pure
        # punctuation ("!!!") still becomes "untitled" rather than a filename
        # made of symbols.
        kept = _ILLEGAL.sub("", text)
        kept = _WHITESPACE.sub("-", kept).strip("-. ")
        slug = kept.lower()

    slug = slug[:max_len].rstrip("-. ")

    if not slug:
        return "untitled"
    if slug.split(".")[0] in _RESERVED:
        return f"{slug}-page"
    return slug


@dataclass
class SyncStats:
    scanned: int = 0
    written: int = 0
    unchanged: int = 0
    removed: int = 0
    truncated: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        bits = [
            f"{self.scanned} pages scanned",
            f"{self.written} written",
            f"{self.unchanged} unchanged",
            f"{self.removed} removed",
        ]
        if self.empty:
            bits.append(f"{len(self.empty)} empty")
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


def _vault_path(
    obj_id: str, index: dict[str, dict], notion_dir: Path, id_chars: int = 8
) -> Path:
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
    # The id suffix is taken from the END of the id. Notion ids in a single
    # workspace share a long leading run - every page here began '28b7c599' or
    # '3e17c599' - so a prefix is close to useless for telling them apart.
    stem = f"{parts[-1] if parts else 'untitled'}-{obj_id[-id_chars:]}"
    return notion_dir.joinpath(*parts[:-1], f"{stem}.md")


def assign_paths(index: dict[str, dict], notion_dir: Path) -> dict[str, Path]:
    """Map every page id to a unique vault path.

    Uniqueness is guaranteed structurally rather than hoped for. A page silently
    overwriting another is the worst failure this module can have: the sync
    reports success, and knowledge quietly disappears from the vault. Colliding
    pages get progressively more of their id until they separate.
    """
    paths: dict[str, Path] = {}
    claimed: dict[str, str] = {}  # lowercased path -> page id that holds it

    for obj_id in sorted(index):  # sorted so results are reproducible
        for id_chars in (8, 12, 16, 24, 32):
            candidate = _vault_path(obj_id, index, notion_dir, id_chars=id_chars)
            # Windows and macOS are case-insensitive; two paths differing only
            # in case would still collide on disk.
            key = str(candidate).lower()
            if key not in claimed:
                claimed[key] = obj_id
                paths[obj_id] = candidate
                break
        else:
            # Two identical full ids is impossible, so this cannot be reached.
            raise RuntimeError(f"could not find a unique path for page {obj_id}")

    return paths


def _relative_link(from_file: Path, to_file: Path) -> str:
    """A POSIX relative link from one mirrored page to another."""
    import os

    rel = os.path.relpath(to_file, from_file.parent).replace("\\", "/")
    return rel if rel.startswith(".") else f"./{rel}"


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
        paths = assign_paths(index, notion_dir)

        for obj_id, obj in index.items():
            live_ids.add(obj_id)
            path = paths[obj_id]
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

            # Rewrite child-page references as links relative to THIS file, so
            # the agent can follow the hierarchy instead of guessing filenames.
            link_targets = {
                other_id: _relative_link(path, other_path)
                for other_id, other_path in paths.items()
                if other_id != obj_id
            }

            body = _frontmatter(obj, rel, content.truncated, content.unknown_block_ids)
            body += clean(content.markdown, link_targets=link_targets)

            # A renamed or moved page would otherwise leave its old file behind.
            if row and row["vault_path"] and Path(row["vault_path"]) != path:
                Path(row["vault_path"]).unlink(missing_ok=True)

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            stats.written += 1
            if not clean(content.markdown).strip():
                # Legitimate for a genuinely blank Notion page, but a large
                # count means the content is not being read at all.
                stats.empty.append(_object_title(obj))
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
