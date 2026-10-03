"""Nightly zips of the game servers' worlds and saves onto the backup drive.

The live installs stay on the SSD; the copies go to ``backups.dir`` (the F:
HDD), one zip per game per run, named by its time so they sort. Nothing here
writes to a source: it reads, and Minecraft's ``backup_hold`` only flushes and
pauses autosave around the copy. The vault push covers none of this.

A game opts in with ``backup_sources(settings)`` - (folder, path inside it)
pairs, empty when there is nothing to keep yet - and optionally
``backup_hold(settings)``, a context manager the copy runs inside. Every game
in ``games.GAMES`` that has the first is backed up.

Skip if unchanged: each zip's comment holds a hash of what went into it, and a
run whose sources hash the same as the newest zip writes nothing. A partial zip
is written as ``.partial`` and renamed only once complete, so a failed run
never looks like a backup. Only the newest ``backups.keep`` zips stay.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import zipfile
from datetime import datetime
from pathlib import Path

from ..config import Settings


log = logging.getLogger(__name__)

HASH_PREFIX = b"qm-sha256:"
# Paper holds an exclusive lock on it while running; it marks a live world, it isn't one.
SKIP_NAMES = frozenset({"session.lock"})


def backup_root(settings: Settings) -> Path:
    return Path(settings.prefs["backups"]["dir"])


def _keep(settings: Settings) -> int:
    return max(1, int(settings.prefs["backups"]["keep"]))


def files_of(sources: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    """Every file under the sources as (path on disk, name in the zip), sorted
    by name so the hash doesn't depend on directory order. Missing sources are
    skipped: a world without a nether is normal."""
    found: dict[str, Path] = {}
    for base, rel in sources:
        top = base / rel
        paths = sorted(p for p in top.rglob("*") if p.is_file()) if top.is_dir() else [top] if top.is_file() else []
        for path in paths:
            if path.name not in SKIP_NAMES:
                found[path.relative_to(base).as_posix()] = path
    return sorted(((p, name) for name, p in found.items()), key=lambda pair: pair[1])


def content_hash(files: list[tuple[Path, str]]) -> str:
    h = hashlib.sha256()
    for path, name in files:
        h.update(name.encode() + b"\0")
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


def zips(folder: Path) -> list[Path]:
    """A game's backups, oldest first (the names are timestamps)."""
    return sorted(folder.glob("*.zip")) if folder.is_dir() else []


def hash_of(zip_path: Path) -> str | None:
    try:
        with zipfile.ZipFile(zip_path) as z:
            comment = z.comment
    except (OSError, zipfile.BadZipFile):
        return None
    return comment[len(HASH_PREFIX):].decode() if comment.startswith(HASH_PREFIX) else None


def _when(zip_path: Path) -> str:
    try:
        return datetime.strptime(zip_path.stem, "%Y-%m-%d_%H%M%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return zip_path.stem


def prune(folder: Path, keep: int) -> list[Path]:
    """Drop all but the newest ``keep`` zips, and any partial a crash left."""
    old = zips(folder)[:-keep]
    for path in [*old, *folder.glob("*.partial")]:
        path.unlink(missing_ok=True)
    return old


def backup_game(settings: Settings, key: str, module, now: datetime | None = None) -> str:
    """One game's backup; returns a line for the summary. Raises on failure."""
    sources = module.backup_sources(settings)
    if not sources:
        return "nothing to back up yet (not set up)"
    folder = backup_root(settings) / key
    hold = getattr(module, "backup_hold", None)
    with hold(settings) if hold else contextlib.nullcontext():
        files = files_of(sources)
        if not files:
            return "nothing to back up yet (no files)"
        digest = content_hash(files)
        existing = zips(folder)
        if existing and hash_of(existing[-1]) == digest:
            prune(folder, _keep(settings))
            return f"unchanged since {_when(existing[-1])}; nothing written"
        folder.mkdir(parents=True, exist_ok=True)
        name = (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S") + ".zip"
        partial = folder / (name + ".partial")
        try:
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                for path, arcname in files:
                    z.write(path, arcname)
                z.comment = HASH_PREFIX + digest.encode()
            partial.replace(folder / name)
        finally:
            partial.unlink(missing_ok=True)
    pruned = prune(folder, _keep(settings))
    size = (folder / name).stat().st_size / 1e6
    return f"backed up {len(files)} files ({size:.1f} MB) to {name}" + (f"; removed {len(pruned)} old" if pruned else "")


def run_backup(settings: Settings, games: dict | None = None) -> tuple[bool, str]:
    """Every game with ``backup_sources``. One failing doesn't stop the others;
    returns (all ok, summary)."""
    if games is None:
        from ..games import GAMES

        games = GAMES
    drive = backup_root(settings).anchor
    if drive and not Path(drive).exists():
        return False, f"Backup drive {drive} isn't there; nothing backed up."
    ok, lines = True, []
    for key, game in games.items():
        module = getattr(game, "module", game)
        if not hasattr(module, "backup_sources"):
            continue
        try:
            line = backup_game(settings, key, module)
        except Exception as exc:  # noqa: BLE001 - unattended: report it, carry on with the next game
            log.exception("backup of %s failed", key)
            ok, line = False, f"FAILED: {exc}"
        lines.append(f"{key}: {line}")
    summary = "\n".join(lines) or "No game servers to back up."
    log.info("game backup: %s", summary.replace("\n", "; "))
    return ok, summary


def last_backup(settings: Settings, key: str) -> str:
    """For the /servers page: when the newest copy was made and how many are kept."""
    folder = backup_root(settings) / key
    existing = zips(folder)
    if not existing:
        return f"No backup yet (nightly, to {folder})."
    return f"Last backup {_when(existing[-1])}, {len(existing)} kept in {folder}."
