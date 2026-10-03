"""Tone: how the bot talks, set by the owner in plain words.

"Change your tone to an angry grandma" in a DM makes the owner agent call the
qm server's ``set_tone`` tool, which overwrites ``System/tone.md``. The owner
and public profiles read it fresh every turn (``Profile.tone_file``), so the
next reply, in a DM, the web chat or a guild channel, already talks that way.
The digest doesn't use it: the owner wants it for chat only.
The file is on ``_PROTECTED``: guild chat reaches the public profile, and an
email must not be able to restyle what friends see. Editable in /settings.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings


HEADER = """# Tone

How Quartermaster talks in DMs, the web chat and server channels (not the
digest). Set it from
a DM ("change your tone to an angry grandma", "back to normal") or edit below.
Empty means its plain default.

"""
MAX_CHARS = 1000

# Always on, tone or not: the owner asked for it (2026-10-03).
PROFANITY = "Swearing and crude language are fine; don't censor or apologise for them."


def tone_path(settings: Settings) -> Path:
    return settings.system_dir / "tone.md"


def read(path: Path | None) -> str:
    """The tone as written, without the header; "" when none is set."""
    if path is None or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if text.startswith(HEADER):
        text = text[len(HEADER):]
    elif text.startswith("# Tone"):
        text = text.split("\n", 1)[-1]
    return text.strip()[:MAX_CHARS]


def write(path: Path, tone: str) -> None:
    """Replace the tone; an empty one resets to the default."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = " ".join(tone.split())
    path.write_text(HEADER + (body + "\n" if body else ""), encoding="utf-8")


def for_prompt(path: Path | None) -> str:
    """The system-prompt block: the profanity line, plus the tone if one is set.
    "" for a profile without a tone file (the JSON-only ones)."""
    if path is None:
        return ""
    tone = read(path)
    if not tone:
        return PROFANITY
    return (
        f"The owner set your tone: {tone}\n"
        "Write every reply in it. It changes how you say things, never what you "
        "do, what's true, or the rules above. " + PROFANITY
    )
