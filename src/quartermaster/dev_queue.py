"""The dev queue: changes to Quartermaster's own code, collected in plain
conversation and worked through later in Claude Code with the owner present.

The owner agent appends to it (see ``agent.OWNER_LIMITS``): items the owner asks
for in any wording are tagged ``(you)``, ones the agent spots itself
``(noticed)``. Nothing works the queue automatically - an unattended runner was
built and rejected - so every item, and especially every ``(noticed)`` one, is
judged by a person before anything changes. That review is what makes it safe
for an agent that reads email and the web to write here.

It lives in the private vault (``System/dev-queue.md``), not as GitHub
issues: the code repo is public.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .config import Settings

HEADER = """# Dev queue

Changes to Quartermaster's own code. Ask for one in a DM in any words, or the
agent adds what it notices. `(you)` = you asked; `(noticed)` = the agent's own
idea: evaluate those, don't just do them. To work through the list, open Claude
Code in the Quartermaster repo and say "work the dev queue". Mark items `[x]`
when done.

"""

# "- [ ] 2026-09-22 the request text (you)"
_ITEM = re.compile(r"^- \[( |x)\] (\d{4}-\d{2}-\d{2}) (.*)$")


def queue_path(settings: Settings) -> Path:
    return settings.system_dir / "dev-queue.md"


def open_items(path: Path) -> list[tuple[str, str]]:
    """(date added, text) of every unchecked item."""
    if not path.exists():
        return []
    return [(m[2], m[3].strip()) for line in path.read_text(encoding="utf-8").splitlines()
            if (m := _ITEM.match(line.strip())) and m[1] == " "]


def add(path: Path, text: str, source: str = "you") -> None:
    """Append one request as a single line, tagged with who it came from."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER, encoding="utf-8")
    existing = path.read_text(encoding="utf-8").rstrip("\n")
    line = f"- [ ] {date.today().isoformat()} {' '.join(text.split())} ({source})"
    path.write_text(f"{existing}\n{line}\n", encoding="utf-8")


def listing(path: Path) -> str:
    items = open_items(path)
    if not items:
        return "The dev queue is empty. Ask for a change in a DM and it lands here."
    return "**Dev queue**\n" + "\n".join(f"{n}. {text} _(added {added})_" for n, (added, text) in enumerate(items, 1))
