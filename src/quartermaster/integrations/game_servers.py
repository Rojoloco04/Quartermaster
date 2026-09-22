"""Every game server Quartermaster runs, for whatever lists them all (the
/servers page in ``qm web``).

A game is a module in ``integrations`` exposing ``status``, ``start``,
``stop``, ``is_running`` and ``log_path`` (all taking ``Settings``) and raising
its own ``RuntimeError`` subclass for anything the owner should read, plus
optionally ``LOG_NOISE``, a regex for console lines not worth showing. Adding a
game is its module plus one line in ``GAMES``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import ModuleType

from . import minecraft, satisfactory


@dataclass(frozen=True)
class Game:
    key: str
    title: str
    module: ModuleType


GAMES: dict[str, Game] = {g.key: g for g in (
    Game("minecraft", "Minecraft (Paper)", minecraft),
    Game("satisfactory", "Satisfactory", satisfactory),
)}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Server consoles colour their output; a browser log box shows the codes raw."""
    return _ANSI.sub("", text)


def clean_console(game: Game, text: str) -> str:
    """Console text for the page: colours stripped, the game's noise lines
    dropped. ``text`` is whole lines (``read_log_from(whole_lines=True)``)."""
    text = strip_ansi(text)
    noise = getattr(game.module, "LOG_NOISE", None)
    if noise is None:
        return text
    return "".join(line for line in text.splitlines(keepends=True) if not noise.search(line))
