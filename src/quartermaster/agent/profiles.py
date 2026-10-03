"""Profiles: who may do what. Each one's working directory, tools, MCP
servers, model limits and instructions. See the package docstring for why
these are the security boundary.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from . import lessons, tone


# Skill is listed explicitly: the SDK's skills="all" would pre-approve it for
# every profile, public and parser included.
VAULT_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "TodoWrite", "Skill"]
RESEARCH_TOOLS = ["WebSearch", "WebFetch"]


MCP_SERVERS = ("google", "microsoft", "spotify", "qm")


def integration_servers() -> dict:
    """The owner's MCP servers, launched with this interpreter.

    ``sys.executable -m`` rather than ``qm.exe``: it survives the venv moving
    and never picks up a different install's qm on PATH.
    """
    return {
        name: {"type": "stdio", "command": sys.executable, "args": ["-m", "quartermaster.cli", "mcp", name]}
        for name in MCP_SERVERS
    }


# `mcp__<server>` pre-approves every tool that server exposes. A headless turn
# has nobody to click "allow", so an unapproved tool is an unusable one.
INTEGRATION_TOOLS = [f"mcp__{name}" for name in integration_servers()]

# Bash is withheld from every Discord profile. The bot is reachable by anyone
# holding the token, and shell access behind a chat message is a far larger
# blast radius than file edits inside one directory. In a terminal the user is
# present and approving, so it stays available there.
NEVER_OVER_CHAT = ["Bash", "NotebookEdit"]

DISCORD_STYLE = (
    "You are replying over Discord. Keep it short - a few sentences unless asked "
    "for more. Discord supports **bold**, *italic*, `code` and lists, but not "
    "tables or headings, so don't use them. Never paste more than a few lines of "
    "a file; summarise and give the path instead."
)

# The owner's, for both chat surfaces. One string on purpose: the system prompt
# is the prompt cache's prefix, so a per-surface style made every switch between
# Discord and the web chat rewrite the whole cached conversation (~$4 a turn).
CHAT_STYLE = (
    "You are replying in a chat (a Discord DM or Quartermaster's web page). Keep "
    "it short - a few sentences unless asked for more. Use **bold**, `code` and "
    "lists; no tables, headings or italics. Never paste more than a few lines of "
    "a file; summarise and give the path instead."
)


# The owner asked this agent to stop a runaway presale ping; it edited
# interests.md, which that job never read, and reported it fixed. It can only
# touch the vault, so it has to say so rather than claim a fix.
OWNER_LIMITS = (
    "You can read and edit files in this vault only. Quartermaster's own code, "
    "its scheduled jobs and its .env live outside it and are beyond your reach. "
    "The owner's Minecraft and Satisfactory servers run on this PC and are "
    "yours to run: the qm minecraft_* and satisfactory_* tools check, start, "
    "stop and save them. "
    "In Notion, the Claude page and its sub-pages are yours: write to them "
    "freely with the qm tools, no permission needed. Any other page is the "
    "owner's, so call propose_notion_edit - it writes nothing, it shows them a "
    "Confirm/Cancel button - and say you've proposed it. To delete any page, "
    "including one under the Claude page, call propose_notion_delete; the same "
    "button applies. Never claim an edit or deletion you only proposed. "
    "You can't change Quartermaster itself and must never report a fix you did "
    "not make. Whenever the owner wants Quartermaster to behave differently, in "
    "any wording, call the qm queue_change tool right away - don't look for the "
    "code or ask them to rephrase - then tell them in one line. Queue things you "
    "notice yourself with source='noticed'. The one exception is how you talk: "
    "when the owner asks for a different tone or persona (\"change your tone "
    "to an angry grandma\", \"back to normal\"), call the qm set_tone tool "
    "instead, and answer in the new tone. Your long-term memory is this "
    "vault's facts/ folder; there is no other memory. "
    "When the owner corrects you - you got a fact wrong, did something they "
    "didn't want, or they tell you how they want something done - call the qm "
    "record_lesson tool right away with the general rule to follow next time, "
    "then fix what's in front of you. "
    "When something you know changes (\"I already have the tickets\"), Grep "
    "facts/ for every place that states it and fix all of them in the same "
    "turn, keeping each fact in one place; if a Notion page states the old "
    "version, propose_notion_edit it. Say in one line what you updated."
)

@dataclass(frozen=True)
class Profile:
    """What one caller is allowed to be.

    ``cwd``, ``tools`` and ``allowed_tools`` are the containment, enforced three
    ways: ``tools`` sets what exists, ``dontAsk`` refuses anything unapproved,
    and ``check_tool`` confines paths. Everything else is ergonomics.
    """

    name: str
    cwd: Path
    # The base set the model is given at all. This is the containment lever:
    # `allowed_tools` only pre-approves, it does NOT control availability, so a
    # profile with allowed_tools=[] still had the full Claude Code toolset in
    # context and reachable. An empty `tools` removes the built-ins entirely.
    tools: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    share_session: bool = False
    system_append: str = ""
    max_turns: int = 30
    enabled: bool = True
    output_schema: dict | None = None
    mcp_servers: dict = field(default_factory=dict)
    # A hard ceiling on one turn. Without this, a hung subprocess or a stalled
    # API call leaves the caller (a Discord "typing..." indicator, most
    # visibly) waiting forever - the SDK does not time out on its own, and a
    # stuck turn produces neither a message nor an exception.
    timeout_seconds: float = 240.0
    # Read fresh on every turn and appended to the system prompt, so a lesson
    # recorded in one DM applies to the very next one without a restart.
    lessons_file: Path | None = None
    # Same, for the reconcile job's open questions (owner only): so "I already
    # have the tickets" is understood as the answer to one of them.
    conflicts_file: Path | None = None
    # Same, for the tone the owner set from a DM: owner and public, so DMs, the
    # web chat and server chat. Never the digest (the owner asked, 2026-10-03).
    tone_file: Path | None = None


def owner_profile(settings: Settings) -> Profile:
    """Full access, shared thread with the terminal. One person only."""
    return Profile(
        name="owner",
        cwd=settings.vault,
        tools=VAULT_TOOLS + RESEARCH_TOOLS,
        allowed_tools=VAULT_TOOLS + RESEARCH_TOOLS + INTEGRATION_TOOLS,
        share_session=True,
        mcp_servers=integration_servers(),
        system_append=CHAT_STYLE + " " + OWNER_LIMITS,
        lessons_file=lessons.lessons_path(settings),
        conflicts_file=settings.system_dir / "conflicts.md",
        tone_file=tone.tone_path(settings),
    )


PUBLIC_ROLE = (
    "You are Quartermaster, a bot in a Discord server of friends. Someone "
    "@mentioned you with something that isn't a moderation or Minecraft request "
    "(those are handled elsewhere, in plain words: \"delete the last 5 messages\", "
    "\"who's on the minecraft server\"), so just talk: answer, joke back, be good "
    "company. Match the channel's tone. You can't take actions (order things, "
    "look things up, message people) and you have no access to your owner's "
    "notes, calendar, email or anything personal: say so plainly if asked. You "
    "may be shown the channel's recent messages first: use them to follow the "
    "conversation (who \"he\" is, what's being judged), but reply only to the "
    "last message, which starts with who sent it. Don't claim you can't see the "
    "chat when those messages are there."
)


def public_profile(settings: Settings) -> Profile:
    """For other people in the server: conversation, nothing else.

    Note what is absent: no vault in ``cwd``, no tools at all, no MCP servers
    and no shared session. It answers guild mentions the parser classes as
    "chat". Anything more is a matter of adding capabilities to an empty list,
    not of removing access from a privileged agent, which is the only ordering
    that fails safe.
    """
    return Profile(
        name="public",
        cwd=settings.public_workspace,
        tools=[],  # no built-ins at all
        allowed_tools=[],
        share_session=False,
        system_append=DISCORD_STYLE + " " + PUBLIC_ROLE,
        max_turns=1,  # no tools, so one turn is the whole reply
        timeout_seconds=60,
        tone_file=tone.tone_path(settings),
    )


def parser_profile(settings: Settings, schema: dict) -> Profile:
    """A profile that can only read a request and emit JSON.

    No tools, no vault, no session. Used to turn an English moderation request
    into a structured plan: the model decides what was *asked for*, and code
    decides what is permitted and what actually runs. Keeping the tool list
    empty is what makes an injected instruction harmless - the worst it can
    produce is a plan a human then rejects.
    """
    return Profile(
        name="parser",
        cwd=settings.public_workspace,
        tools=[],  # it reads a request and emits JSON; it needs nothing else
        allowed_tools=[],
        share_session=False,
        # Structured output arrives as a StructuredOutput tool call; one that
        # fails schema validation is retried in a second turn. At 1, that retry
        # failed the whole request ("Reached maximum number of turns", 7 times
        # in the log by 2026-09-22). Same allowance as reconcile.
        max_turns=3,
        output_schema=schema,
        # Parsing English into JSON should take seconds. It also blocks a live
        # moderation request someone is waiting on in-channel, so it gets a
        # much shorter leash than a research-heavy owner turn.
        timeout_seconds=45.0,
    )


def digest_profile(settings: Settings, schema: dict) -> Profile:
    """Picks the digest's events from candidates collectors already gathered,
    answering in JSON (``schema``); code renders the message.

    No tools: by the time this profile is asked anything, the prompt holds
    everything it needs. Its ``cwd`` is NOT the vault: every run leaves a
    session file for its cwd, and ``--continue`` resumes the newest one, so a
    digest run there made the owner's next DM continue the digest instead of
    their own thread. The vault's CLAUDE.md is passed in directly instead.
    """
    claude_md = settings.vault / "CLAUDE.md"
    rules = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    return Profile(
        name="digest",
        cwd=settings.workspace("digest"),
        tools=[],
        allowed_tools=[],
        share_session=False,
        # A structured answer can take a second turn (see reconcile_profile).
        max_turns=3,
        output_schema=schema,
        system_append=f"The owner's standing instructions:\n{rules}" if rules else "",
        lessons_file=lessons.lessons_path(settings),
        timeout_seconds=600.0,
    )


def tidy_profile(settings: Settings) -> Profile:
    """Rewrites the Claude page without its stale parts. No tools: it is handed
    the page and returns a new one, and what it returns is proposed for the
    owner to confirm, never applied."""
    return Profile(
        name="tidy",
        cwd=settings.workspace("tidy"),
        tools=[],
        allowed_tools=[],
        share_session=False,
        max_turns=1,
        timeout_seconds=600.0,
    )


def reconcile_profile(settings: Settings, schema: dict) -> Profile:
    """Reads the facts and the Notion mirror, returns edits and conflicts as
    JSON. No tools: code decides which edits are safe to apply."""
    return Profile(
        name="reconcile",
        cwd=settings.workspace("reconcile"),
        tools=[],
        allowed_tools=[],
        share_session=False,
        # 1 was refused live ("Reached maximum number of turns"): a long JSON
        # answer can take the structured-output step a second turn.
        max_turns=3,
        output_schema=schema,
        timeout_seconds=600.0,
    )
