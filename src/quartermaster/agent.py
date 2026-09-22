"""The Agent SDK wrapper.

Every surface goes through here, so tool permissions and model choice are
decided in one place rather than scattered across callers.

**Profiles are the security boundary.** The bot lives in a normal Discord server
where other people can eventually talk to it, and it can read a vault full of
personal knowledge. A prompt instruction not to discuss that knowledge is not a
boundary — anyone who can send text can argue with an instruction. So each
profile carries its own working directory and its own tool list, and a profile
without vault tools, rooted outside the vault, physically cannot reach it no
matter what it is asked.

**Session sharing.** The owner profile runs with ``cwd`` set to the vault, the
same as ``claude`` in a terminal, so both write transcripts to
``~/.claude/projects/<encoded-vault-path>/``. With
``continue_conversation=True`` each surface picks up whatever the other said
last: message the bot from your phone, then run ``claude --continue`` in the
vault and the thread is there.

That is also why this does not hold a long-lived ``ClaudeSDKClient``. A
persistent client keeps its own session and would never notice anything said in
the terminal — faster, and it would quietly break the one property that makes
the two surfaces feel like one assistant.

Sync is turn-level, not live. Neither side sees the other mid-turn.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import uuid
from contextlib import aclosing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from . import lessons
from .config import Settings

log = logging.getLogger(__name__)

# Called as the turn runs: ("text", str) for each block of reply text, and
# ("tool", (name, input)) for each tool call. Lets a surface stream the reply
# instead of going silent until the whole turn is done.
Progress = Callable[[str, object], Awaitable[None]]

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

# File tools and the input key holding the path they touch.
_PATH_KEYS = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "Glob": "path", "Grep": "path"}

# Inside the vault, but writing here is code execution on a later run: hooks
# and MCP servers are launched from .claude/ and .mcp.json, git hooks from .git/.
_PROTECTED = (".claude", ".mcp.json", ".git", ".githooks",
              # Written only through the qm server's queue_change tool, so every
              # entry is one tagged line the owner reviews before acting on it.
              "90-System/dev-queue.md")

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
    "notice yourself with source='noticed'. Your long-term memory is this "
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
    )


def public_profile(settings: Settings) -> Profile:
    """For other people in the server. Deliberately inert until built out.

    Note what is absent: no vault in ``cwd``, no file tools at all, and no shared
    session. Turning this on is a matter of adding capabilities to an empty list,
    not of removing access from a privileged agent — which is the only ordering
    that fails safe.
    """
    return Profile(
        name="public",
        cwd=settings.public_workspace,
        tools=[],  # no built-ins at all
        allowed_tools=[],
        share_session=False,
        system_append=(
            DISCORD_STYLE
            + " You are talking to someone who is not your owner. You have no "
            "access to their notes or personal information and should say so "
            "plainly if asked."
        ),
        max_turns=8,
        enabled=False,
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
        max_turns=1,
        output_schema=schema,
        # Parsing English into JSON should take seconds. It also blocks a live
        # moderation request someone is waiting on in-channel, so it gets a
        # much shorter leash than a research-heavy owner turn.
        timeout_seconds=45.0,
    )


def digest_profile(settings: Settings) -> Profile:
    """Writes the weekly digest's prose from data collectors already gathered.

    No tools: collectors are plain Python that do no reasoning, so by the time
    this profile is asked anything it has everything it needs in the prompt.
    Its ``cwd`` is NOT the vault: every run leaves a session file for its cwd,
    and ``--continue`` resumes the newest one, so a digest run there made the
    owner's next DM continue the digest instead of their own thread. The
    vault's CLAUDE.md (tone rules) is passed in directly instead.
    """
    claude_md = settings.vault / "CLAUDE.md"
    rules = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    return Profile(
        name="digest",
        cwd=settings.workspace("digest"),
        tools=[],
        allowed_tools=[],
        share_session=False,
        max_turns=1,
        system_append=DISCORD_STYLE + (f"\n\nThe owner's standing instructions:\n{rules}" if rules else ""),
        lessons_file=lessons.lessons_path(settings),
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


@dataclass
class Reply:
    text: str
    structured: dict | None = None
    session_id: str | None = None
    cost_usd: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# Model IDs, not aliases ("sonnet", "opus", ...) - deterministic regardless
# of what an alias currently resolves to for this CLI install.
HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"

# Thinking depth for every Sonnet/Opus turn. Chat, a digest and a reconcile
# are not hard reasoning; "opus:" is there when one is.
EFFORT = "medium"

_OVERRIDE_TAGS = {"opus:": OPUS, "sonnet:": SONNET, "haiku:": HAIKU}

# What each tier falls back to if the primary is down or overloaded (a 529
# has been seen mid-way through an ordinary DM). One step toward the middle
# tier rather than always-down or always-up: Opus keeps most of its quality
# by dropping to Sonnet, a stuck Sonnet turn still gets answered rather than
# left hanging, and Haiku - already the floor - steps up rather than nowhere.
_FALLBACK = {OPUS: SONNET, SONNET: HAIKU, HAIKU: SONNET}


def pick_model(prompt: str, profile: Profile) -> str:
    """The model for one turn: fixed per profile, overridable per message by
    starting it with "opus:", "sonnet:" or "haiku:".

    The owner used to be routed per message (Haiku for quick lookups, Opus for
    hard ones). In a long shared session that cost more than it saved: each
    model has its own prompt cache, so every switch re-sent the whole
    conversation uncached (one Haiku lookup wrote 158k tokens).
    """
    lower = prompt.strip().lower()

    for tag, tier in _OVERRIDE_TAGS.items():
        if lower.startswith(tag):
            return tier

    if profile.name == "parser":
        # Fixed-schema extraction, one turn, no tools - code validates every
        # field afterwards, so reasoning depth buys nothing here.
        return HAIKU
    if profile.name == "public":
        # Low-stakes and already contained by an empty tool list either way.
        return HAIKU
    if profile.name in ("digest", "tidy", "reconcile"):
        # A structured data dump or a whole page to rewrite, not conversational
        # text - the word-count and keyword heuristics below were built to read
        # a chat message. Fixed at Sonnet rather than guessed.
        return SONNET

    return SONNET


def _options(profile: Profile, prompt: str = "", cli_path: str | None = None) -> ClaudeAgentOptions:
    extra: dict[str, object] = {}
    if cli_path:
        # Must be a real executable. The SDK refuses .cmd/.bat wrappers on
        # Windows - cmd.exe can execute commands injected through arguments and
        # there is no reliable escaping for it - so the npm shim will not do.
        extra["cli_path"] = cli_path

    model = pick_model(prompt, profile)
    from . import reconcile  # imports agent; resolved at call time

    append = "\n\n".join(p for p in (
        profile.system_append,
        lessons.for_prompt(profile.lessons_file),
        reconcile.for_prompt(profile.conflicts_file),
    ) if p)
    return ClaudeAgentOptions(
        cwd=str(profile.cwd),
        model=model,
        fallback_model=_FALLBACK[model],
        **extra,
        # Loads CLAUDE.md and .claude/ from cwd - the same configuration the CLI
        # reads. For the public profile that directory is not the vault, so none
        # of the vault's context is loaded either. Not "user": ~/.claude is the
        # owner's coding setup (skills, plugins, rules, and Sonnet at xhigh
        # effort), all of it tokens on every turn and none of it for chat.
        setting_sources=["project"],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            **({"append": append} if append else {}),
        },
        tools=profile.tools,
        allowed_tools=profile.allowed_tools,
        mcp_servers=profile.mcp_servers,
        # Only the servers above. Without these two the CLI also loaded the
        # account's claude.ai connectors (Notion, Gmail, Drive, ...) and any
        # user-level servers: ~120k tokens of tool schemas on every call, none
        # of them usable under dontAsk.
        strict_mcp_config=True,
        env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
        disallowed_tools=NEVER_OVER_CHAT,
        # Anything not pre-approved is refused rather than left "ask"-able:
        # the CLI also loads the account's claude.ai connectors (Gmail send,
        # Drive share, Notion edit), and none of those belong to any profile.
        permission_mode="dontAsk",
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_guard(profile)])]},
        continue_conversation=profile.share_session,
        max_turns=profile.max_turns,
        # Explicit, so no settings file can raise it. Haiku doesn't take one.
        **({"effort": EFFORT} if model != HAIKU else {}),
        **(
            {"output_format": {"type": "json_schema", "schema": profile.output_schema}}
            if profile.output_schema
            else {}
        ),
    )


def check_tool(profile: Profile, tool: str, tool_input: dict) -> str | None:
    """Why this call is refused, or None if it may run.

    The second line of defence behind ``tools``/``dontAsk``, and the only one
    that looks at arguments: file tools stay inside ``profile.cwd``, and never
    touch the files that would run code later (see ``_PROTECTED``). A prompt
    injection in an email or web page can ask for anything; this is what it
    runs into.
    """
    allowed = tool in profile.allowed_tools or any(
        a.startswith("mcp__") and tool.startswith(a + "__") for a in profile.allowed_tools
    ) or (
        # How the SDK delivers output_schema answers: it only returns data. It
        # was denied once, and the reconcile job looped until max_turns.
        tool == "StructuredOutput" and profile.output_schema is not None
    )
    if not allowed or tool in NEVER_OVER_CHAT:
        return f"{tool} is not available to the {profile.name} profile."

    if tool in ("Glob", "Grep"):
        # A pattern is a path too: Glob("C:/Users/**") once searched the whole
        # home directory with no `path` given.
        pattern = str(tool_input.get("pattern" if tool == "Glob" else "glob") or "")
        if PureWindowsPath(pattern).anchor or pattern.startswith(("/", "\\", "~")) or ".." in re.split(r"[\\/]", pattern):
            return f"{tool} patterns must be relative to {profile.cwd.resolve()}."

    key = _PATH_KEYS.get(tool)
    if key is None or not tool_input.get(key):
        return None  # no path given: the tool defaults to cwd
    root = profile.cwd.resolve()
    target = (root / str(tool_input[key])).resolve()
    if target != root and root not in target.parents:
        return f"{tool} is limited to {root}."
    rel = target.relative_to(root).as_posix()
    if tool not in ("Read", "Glob", "Grep") and any(rel == p or rel.startswith(p + "/") for p in _PROTECTED):
        return f"{tool} may not change {rel} - it controls what runs later, or is the owner's alone to write."
    return None


def _guard(profile: Profile):
    async def hook(input_data: dict, tool_use_id: str | None, context: object) -> dict:
        reason = check_tool(profile, input_data.get("tool_name", ""), input_data.get("tool_input") or {})
        if reason is None:
            return {}
        log.warning("denied %s for %s: %s", input_data.get("tool_name"), profile.name, reason)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return hook


def _trunc(value: object, limit: int = 300) -> str:
    """A log-safe rendering of a tool call's input or result - readable, not
    a full file dump or email body flooding the log for one turn."""
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + f"...[{len(text) - limit} more chars]"


async def ask(
    prompt: str, profile: Profile, cli_path: str | None = None, on_progress: Progress | None = None
) -> Reply:
    """Send one message and collect the reply.

    Never raises. A surface that dies on a bad turn is worse than one that says
    it failed, because the user is left wondering whether the message landed.
    """
    if not profile.enabled:
        return Reply(text="", error="That profile is not enabled.")

    profile.cwd.mkdir(parents=True, exist_ok=True)

    # Ties every log line this turn produces - the prompt, each tool call and
    # result, the outcome - together in one grep, without waiting on the
    # SDK's own session_id, which isn't known until the ResultMessage lands.
    # This is the audit trail: nothing the agent does happens off the record.
    turn_id = uuid.uuid4().hex[:8]
    log.info(
        "[%s] %s turn start (model=%s): %s",
        turn_id, profile.name, pick_model(prompt, profile), _trunc(prompt, 200),
    )

    chunks: list[str] = []
    session_id: str | None = None
    cost: float | None = None
    structured: dict | None = None
    error: str | None = None

    async def emit(kind: str, payload: object) -> None:
        if on_progress is None:
            return
        try:
            await on_progress(kind, payload)
        except Exception:  # noqa: BLE001 - a failed status update must not end the turn
            log.exception("[%s] progress callback failed", turn_id)

    async def _drain() -> None:
        nonlocal session_id, cost, structured, error
        # Drain the stream to completion rather than returning from inside it.
        # Returning early leaves the SDK's async generator suspended and closing
        # it then raises "aclose(): asynchronous generator is already running" -
        # once per call, on a path that runs on every moderation request. The
        # timeout below is not that bug: wait_for cancels this coroutine with
        # an ordinary CancelledError at whatever await it's sitting on, the
        # same as any other task cancellation, rather than walking away and
        # leaving the generator suspended with nothing ever delivered to it.
        # aclosing: a cancelled turn (timeout, !stop) closes the SDK stream, and
        # with it the CLI subprocess, now rather than whenever it is collected.
        async with aclosing(query(prompt=prompt, options=_options(profile, prompt, cli_path))) as stream:
            async for message in stream:
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
                            await emit("text", block.text)
                        elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                            log.info("[%s] tool call: %s(%s)", turn_id, block.name, _trunc(block.input))
                            await emit("tool", (block.name, block.input))
                elif isinstance(message, UserMessage):
                    # Tool results are replayed back through the stream as a
                    # "user" turn - this is the only place a tool's outcome
                    # (including a failure the model then has to react to) shows
                    # up at all.
                    for block in message.content if isinstance(message.content, list) else []:
                        if isinstance(block, (ToolResultBlock, ServerToolResultBlock)):
                            status = "error" if getattr(block, "is_error", False) else "ok"
                            log.info("[%s] tool result (%s): %s", turn_id, status, _trunc(block.content))
                elif isinstance(message, ResultMessage):
                    session_id = message.session_id
                    cost = getattr(message, "total_cost_usd", None)
                    # With output_format set the model answers through a
                    # StructuredOutput tool call and no TextBlock is ever emitted,
                    # so the payload appears only here.
                    candidate = getattr(message, "structured_output", None)
                    if isinstance(candidate, dict):
                        structured = candidate
                    if message.subtype != "success":
                        error = _explain(message.subtype)

    try:
        await asyncio.wait_for(_drain(), timeout=profile.timeout_seconds)
    except asyncio.TimeoutError:
        # No exception from the SDK, no result message - it just never came
        # back. Without this, the caller (a Discord "typing..." indicator,
        # most visibly) waits forever: nothing else here ever un-hangs it.
        log.warning("[%s] timed out after %.0fs (profile=%s)", turn_id, profile.timeout_seconds, profile.name)
        return Reply(
            text="".join(chunks).strip(),
            error=(
                f"No response after {profile.timeout_seconds:.0f}s - giving up rather than "
                "hanging. Try again; if it keeps happening, check https://status.claude.com/."
            ),
        )
    except asyncio.CancelledError:
        log.info("[%s] cancelled (profile=%s)", turn_id, profile.name)
        raise
    except Exception as exc:  # noqa: BLE001 - surfaces must report, not crash
        log.exception("[%s] agent query failed (profile=%s)", turn_id, profile.name)
        return Reply(text="".join(chunks).strip(), error=f"{type(exc).__name__}: {exc}")

    log.info(
        "[%s] turn done: ok=%s cost=$%s session=%s",
        turn_id, error is None, f"{cost:.4f}" if cost is not None else "?", session_id,
    )
    return Reply(
        text="".join(chunks).strip(),
        structured=structured,
        session_id=session_id,
        cost_usd=cost,
        error=error,
    )


def _explain(subtype: str) -> str:
    """Turn an SDK result subtype into something worth reading in Discord."""
    return {
        "error_max_turns": "I hit the turn limit on that one. Ask me for a smaller piece of it.",
        "error_max_budget_usd": "I hit the spend cap for that request.",
        "error_during_execution": "Something failed mid-way through that.",
    }.get(subtype, f"The request ended as '{subtype}'.")


def transcript_dir(cwd: Path) -> Path:
    """Where a profile's session files live.

    Claude Code encodes the working directory by replacing every non-alphanumeric
    character with '-'. Useful for confirming the owner profile and the terminal
    really are pointed at the same place when session sharing looks wrong.
    """
    encoded = "".join(c if c.isalnum() else "-" for c in str(cwd.resolve()))
    return Path.home() / ".claude" / "projects" / encoded
