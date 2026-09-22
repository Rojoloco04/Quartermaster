"""The dev queue: changes to Quartermaster's own code, jotted down from Discord
and worked through later in Claude Code, with the owner present and approving.

Adding is ``!queue <text>`` in the owner's DM, handled in code
(``discord_bot.command``), never by the model, and the file is on
``agent._PROTECTED`` so no agent can write it. What's queued is therefore
exactly what the owner typed. Working the queue is deliberately not automated:
an unattended agent with a shell and push access was considered and rejected.

It lives in the private vault (``90-System/dev-queue.md``), not as GitHub
issues: the code repo is public.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .config import Settings

HEADER = """# Dev queue

Changes to Quartermaster's own code, added with `!queue <text>` in a DM. To work
through them, open Claude Code in the Quartermaster repo and say "work the dev
queue in <this file's path>". Mark items `[x]` when done. No agent can edit this
file from Discord; you and a terminal Claude Code session can.

"""

# "- [ ] 2026-09-22 the request text"
_ITEM = re.compile(r"^- \[( |x)\] (\d{4}-\d{2}-\d{2}) (.*)$")


def queue_path(settings: Settings) -> Path:
    return settings.system_dir / "dev-queue.md"


def open_items(path: Path) -> list[tuple[str, str]]:
    """(date added, text) of every unchecked item."""
    if not path.exists():
        return []
    return [(m[2], m[3].strip()) for line in path.read_text(encoding="utf-8").splitlines()
            if (m := _ITEM.match(line.strip())) and m[1] == " "]


def add(path: Path, text: str) -> None:
    """Append the owner's request verbatim, as one line."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER, encoding="utf-8")
    existing = path.read_text(encoding="utf-8").rstrip("\n")
    path.write_text(f"{existing}\n- [ ] {date.today().isoformat()} {' '.join(text.split())}\n", encoding="utf-8")


def listing(path: Path) -> str:
    items = open_items(path)
    if not items:
        return "The dev queue is empty. Add to it with `!queue <what to change>`."
    return "**Dev queue**\n" + "\n".join(f"{n}. {text} _(added {added})_" for n, (added, text) in enumerate(items, 1))
