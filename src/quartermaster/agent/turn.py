"""One turn: build the SDK options for a profile, pick the model, run the
query, stream progress, and return a ``Reply``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    query,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from . import lessons, tone
from .guard import _guard
from .profiles import NEVER_OVER_CHAT, Profile


# The dashboard's turn table parses log lines by this name.
log = logging.getLogger("quartermaster.agent")


# Called as the turn runs: ("text", str) for each block of reply text, and
# ("tool", (name, input)) for each tool call. Lets a surface stream the reply
# instead of going silent until the whole turn is done.
Progress = Callable[[str, object], Awaitable[None]]


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
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"

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
    from ..knowledge import reconcile  # imports agent; resolved at call time

    append = "\n\n".join(p for p in (
        profile.system_append,
        lessons.for_prompt(profile.lessons_file),
        reconcile.for_prompt(profile.conflicts_file),
        tone.for_prompt(profile.tone_file),
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
