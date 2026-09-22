"""What the owner's two chat surfaces share: the Discord DM and ``qm web``'s chat.

Both run owner turns in the one shared session, from two processes (the bot
and the dashboard). Two turns at once would both resume the newest session and
interleave, so ``TurnLock`` allows one owner turn at a time across processes.
The session controls ("stop", "start fresh") and the status-line wording live
here so the two surfaces behave the same.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from pathlib import Path, PurePath
from urllib.parse import urlparse

from ..agent import Profile, transcript_dir
from ..config import Settings, current_prefs

log = logging.getLogger(__name__)

# Whole-message session controls. `!stop`/`!new` still work as aliases.
_STOP = re.compile(r"(stop|cancel|abort|nvm|never ?mind|forget it|hold on|wait,? stop|stop that)[.! ]*")
_NEW = re.compile(
    r"(new (chat|conversation|thread|topic)|start (over|fresh|a new (chat|conversation|thread))"
    r"|fresh (start|chat|conversation)|clean slate|reset( the)? (chat|conversation))[.! ]*"
)

FRESH_NOTE = ("Your next message starts a fresh conversation. The old one is still "
              "there: `claude --resume` in the vault lists it.")


def session_control(text: str) -> str | None:
    """"stop", "new", or None. Only a whole, short message counts, so "stop
    reminding me about X" is still an ordinary request for the model."""
    word = text.strip().lower()
    if word == "!stop" or _STOP.fullmatch(word):
        return "stop"
    if word == "!new" or _NEW.fullmatch(word):
        return "new"
    return None


def idle_minutes(settings: Settings, now: float | None = None) -> float | None:
    """Minutes since anything was said in the vault's sessions (Discord, the
    web chat or the terminal), or None if there are none."""
    folder = transcript_dir(settings.vault)
    times = [p.stat().st_mtime for p in folder.glob("*.jsonl")] if folder.exists() else []
    return ((now or time.time()) - max(times)) / 60 if times else None


def continue_or_fresh(settings: Settings, profile: Profile, now: float | None = None) -> Profile:
    """The profile for this turn: a fresh session after ``chat.fresh_after_minutes``
    of quiet, else the shared one. Continuing re-sends the whole conversation
    each turn, and after a pause the prompt cache has to be rebuilt for it too.
    Read from config.toml each turn, so a change applies without a restart."""
    limit = current_prefs(settings).get("chat", {}).get("fresh_after_minutes", 5)
    if not profile.share_session or not limit:
        return profile
    idle = idle_minutes(settings, now)
    if idle is None or idle < limit:
        return profile
    log.info("no chat activity for %.0f min: starting a fresh session", idle)
    return replace(profile, share_session=False)


def describe_tool(name: str, tool_input: dict) -> str:
    """One short line for the status message: what the agent is doing now."""
    path = tool_input.get("file_path") or ""
    fixed = {
        "Glob": "Searching the vault", "Grep": "Searching the vault",
        "TodoWrite": "Planning", "Skill": "Using a skill",
    }
    if name in fixed:
        return fixed[name]
    if name == "Read":
        return f"Reading `{PurePath(path).name}`"
    if name in ("Write", "Edit"):
        return f"Editing `{PurePath(path).name}`"
    if name == "WebSearch":
        return f"Searching the web for \"{str(tool_input.get('query', ''))[:60]}\""
    if name == "WebFetch":
        return f"Reading {urlparse(str(tool_input.get('url', ''))).netloc or 'a web page'}"
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        if server == "google":
            return "Checking email" if "email" in tool else "Checking your calendar"
        return {"microsoft": "Checking To Do", "spotify": "Checking Spotify"}.get(server, f"Using {tool}")
    return f"Using {name}"


def lock_path(settings: Settings) -> Path:
    return settings.system_dir / "turn.lock"


class TurnLock:
    """One owner turn at a time, across the bot and the dashboard.

    An OS file lock, so a process that dies releases it: nothing stale to clear.
    Non-blocking by design - a surface that finds it held says it's busy rather
    than queueing a message the owner may already have given up on.
    """

    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        try:
            _lock(f)
        except OSError:
            f.close()
            return False
        self._file = f
        return True

    def release(self) -> None:
        if self._file is not None:
            try:
                _unlock(self._file)
            finally:
                self._file.close()
                self._file = None


try:
    import msvcrt

    def _lock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
except ImportError:  # not Windows
    import fcntl

    def _lock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
