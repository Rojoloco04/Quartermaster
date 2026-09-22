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
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

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


MCP_SERVERS = ("google", "microsoft", "spotify", "notion")


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
_PROTECTED = (".claude", ".mcp.json", ".git")

DISCORD_STYLE = (
    "You are replying over Discord. Keep it short - a few sentences unless asked "
    "for more. Discord supports **bold**, *italic*, `code` and lists, but not "
    "tables or headings, so don't use them. Never paste more than a few lines of "
    "a file; summarise and give the path instead."
)


# The owner asked this agent to stop a runaway presale ping; it edited
# interests.md, which that job never read, and reported it fixed. It can only
# touch the vault, so it has to say so rather than claim a fix.
OWNER_LIMITS = (
    "You can read and edit files in this vault only. Quartermaster's own code, "
    "its scheduled jobs and its .env live outside it and are beyond your reach. "
    "In Notion you may write only to the Claude page and its sub-pages, using "
    "the notion tools; everything else there is a proposal in pending.md. "
    "If asked to fix or change how Quartermaster itself behaves, say plainly "
    "that you can't, and describe the change for the owner to make in Claude "
    "Code. Never report a fix you did not make and verify."
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


def owner_profile(settings: Settings) -> Profile:
    """Full access, shared thread with the terminal. One person only."""
    return Profile(
        name="owner",
        cwd=settings.vault,
        tools=VAULT_TOOLS + RESEARCH_TOOLS,
        allowed_tools=VAULT_TOOLS + RESEARCH_TOOLS + INTEGRATION_TOOLS,
        share_session=True,
        mcp_servers=integration_servers(),
        system_append=DISCORD_STYLE + " " + OWNER_LIMITS,
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
    ``cwd`` is still the vault so CLAUDE.md's tone rules load the normal way,
    even though an empty ``tools`` list means nothing can actually be read.
    Not a shared session - a weekly structured-data dump doesn't belong in the
    thread the owner continues from the terminal.
    """
    return Profile(
        name="digest",
        cwd=settings.vault,
        tools=[],
        allowed_tools=[],
        share_session=False,
        max_turns=1,
        system_append=DISCORD_STYLE,
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

# Content signals that a message needs Opus-level reasoning rather than
# Sonnet's default. Deliberately narrow: a missed signal just runs on
# Sonnet, which is usually fine; a false positive spends ~2.5x for nothing.
_OPUS_SIGNALS = (
    "architecture", "refactor", "trade-off", "tradeoff", "root cause",
    "think hard", "think carefully", "design a", "security review",
    "debug", "deep dive", "plan out",
)

# Short single-fact lookups where Haiku's speed matters more than Sonnet's
# extra reasoning - the owner profile still validates nothing it says, but
# these are the kind of question a wrong answer to gets noticed immediately.
_HAIKU_STARTS = (
    "what's on", "what is on", "when is", "when's", "what time",
    "remind me", "list my", "mark ", "complete ",
)

_OVERRIDE_TAGS = {"opus:": OPUS, "sonnet:": SONNET, "haiku:": HAIKU}

# What each tier falls back to if the primary is down or overloaded (a 529
# has been seen mid-way through an ordinary DM). One step toward the middle
# tier rather than always-down or always-up: Opus keeps most of its quality
# by dropping to Sonnet, a stuck Sonnet turn still gets answered rather than
# left hanging, and Haiku - already the floor - steps up rather than nowhere.
_FALLBACK = {OPUS: SONNET, SONNET: HAIKU, HAIKU: SONNET}


def pick_model(prompt: str, profile: Profile) -> str:
    """Route one turn to the cheapest model likely to do it justice.

    A heuristic, not a classifier call - an extra request just to decide the
    model would cost as much as a cheap turn itself, working against the
    point of routing. Wrong guesses are cheap to correct: start a message
    with "opus:", "sonnet:" or "haiku:" to force that tier for one turn.
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
    if profile.name == "digest":
        # A structured data dump, not conversational text - the word-count and
        # keyword heuristics below were built to read a chat message, not this.
        # Fixed at Sonnet rather than guessed.
        return SONNET

    word_count = len(lower.split())
    if word_count > 300 or any(signal in lower for signal in _OPUS_SIGNALS):
        return OPUS
    if word_count <= 12 and any(lower.startswith(s) for s in _HAIKU_STARTS):
        return HAIKU
    return SONNET


def _options(profile: Profile, prompt: str = "", cli_path: str | None = None) -> ClaudeAgentOptions:
    extra: dict[str, object] = {}
    if cli_path:
        # Must be a real executable. The SDK refuses .cmd/.bat wrappers on
        # Windows - cmd.exe can execute commands injected through arguments and
        # there is no reliable escaping for it - so the npm shim will not do.
        extra["cli_path"] = cli_path

    model = pick_model(prompt, profile)
    return ClaudeAgentOptions(
        cwd=str(profile.cwd),
        model=model,
        fallback_model=_FALLBACK[model],
        **extra,
        # Loads CLAUDE.md and .claude/ from cwd - the same configuration the CLI
        # reads. For the public profile that directory is not the vault, so none
        # of the vault's context is loaded either.
        setting_sources=["user", "project"],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            **({"append": profile.system_append} if profile.system_append else {}),
        },
        tools=profile.tools,
        allowed_tools=profile.allowed_tools,
        mcp_servers=profile.mcp_servers,
        disallowed_tools=NEVER_OVER_CHAT,
        # Anything not pre-approved is refused rather than left "ask"-able:
        # the CLI also loads the account's claude.ai connectors (Gmail send,
        # Drive share, Notion edit), and none of those belong to any profile.
        permission_mode="dontAsk",
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_guard(profile)])]},
        continue_conversation=profile.share_session,
        max_turns=profile.max_turns,
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
    )
    if not allowed or tool in NEVER_OVER_CHAT:
        return f"{tool} is not available to the {profile.name} profile."

    key = _PATH_KEYS.get(tool)
    if key is None or not tool_input.get(key):
        return None  # no path given: the tool defaults to cwd
    root = profile.cwd.resolve()
    target = (root / str(tool_input[key])).resolve()
    if target != root and root not in target.parents:
        return f"{tool} is limited to {root}."
    rel = target.relative_to(root).parts
    if rel and rel[0] in _PROTECTED and tool not in ("Read", "Glob", "Grep"):
        return f"{tool} may not change {rel[0]} - it controls what runs on the next start."
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
        async for message in query(prompt=prompt, options=_options(profile, prompt, cli_path)):
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
