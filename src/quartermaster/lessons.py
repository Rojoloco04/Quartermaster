"""Lessons: what the owner has corrected, kept so it stays corrected.

When the owner says the agent got something wrong, the agent calls the qm
server's ``record_lesson`` tool (a tool, because conventions get forgotten and
tools get used - see the dev queue's history) and a dated line lands in
``facts/lessons.md``. Every owner turn and every digest reads the file fresh
(``Profile.lessons_file``), so a correction made in one DM shapes the next
reply and the next digest. Plain markdown: the owner can read, fix or delete a
lesson in ``qm web`` like any other fact.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .config import Settings

HEADER = """# Lessons

Corrections you've given, recorded when you gave them. Every reply and digest
follows these. Edit or delete a line and it's gone for good.

"""
MAX_CHARS = 6000  # what goes into a prompt; the newest lessons win if it's longer


def lessons_path(settings: Settings) -> Path:
    return settings.facts_dir / "lessons.md"


def add(path: Path, lesson: str) -> bool:
    """Append one lesson as a dated bullet. False if it's already there."""
    text = " ".join(lesson.split())
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER, encoding="utf-8")
    existing = path.read_text(encoding="utf-8")
    if text.lower() in existing.lower():
        return False
    path.write_text(f"{existing.rstrip()}\n- {date.today().isoformat()}: {text}\n", encoding="utf-8")
    return True


def for_prompt(path: Path | None) -> str:
    """The lessons as a system-prompt block, or "" when there are none."""
    if path is None or not path.exists():
        return ""
    bullets = [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")]
    if not bullets:
        return ""
    body = "\n".join(bullets)
    if len(body) > MAX_CHARS:
        body = body[-MAX_CHARS:].split("\n", 1)[-1]
    return "Lessons from the owner's past corrections. Follow them; they outrank your defaults:\n" + body
