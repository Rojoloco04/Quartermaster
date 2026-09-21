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

import logging
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from .config import Settings

log = logging.getLogger(__name__)

VAULT_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "TodoWrite"]
RESEARCH_TOOLS = ["WebSearch", "WebFetch"]

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


@dataclass(frozen=True)
class Profile:
    """What one caller is allowed to be.

    ``cwd`` and ``allowed_tools`` together are the containment. Everything else
    is ergonomics.
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


def owner_profile(settings: Settings) -> Profile:
    """Full access, shared thread with the terminal. One person only."""
    return Profile(
        name="owner",
        cwd=settings.vault,
        tools=VAULT_TOOLS + RESEARCH_TOOLS,
        allowed_tools=VAULT_TOOLS + RESEARCH_TOOLS,
        share_session=True,
        system_append=DISCORD_STYLE,
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


def _options(profile: Profile, cli_path: str | None = None) -> ClaudeAgentOptions:
    extra: dict[str, object] = {}
    if cli_path:
        # Must be a real executable. The SDK refuses .cmd/.bat wrappers on
        # Windows - cmd.exe can execute commands injected through arguments and
        # there is no reliable escaping for it - so the npm shim will not do.
        extra["cli_path"] = cli_path

    return ClaudeAgentOptions(
        cwd=str(profile.cwd),
        **extra,
        # Loads CLAUDE.md and .claude/ from cwd - the same configuration the CLI
        # reads. For the public profile that directory is not the vault, so none
        # of the vault's context is loaded either.
        setting_sources=["user", "project"],
        skills="all",
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            **({"append": profile.system_append} if profile.system_append else {}),
        },
        tools=profile.tools,
        allowed_tools=profile.allowed_tools,
        disallowed_tools=NEVER_OVER_CHAT,
        permission_mode="acceptEdits",
        continue_conversation=profile.share_session,
        max_turns=profile.max_turns,
        **(
            {"output_format": {"type": "json_schema", "schema": profile.output_schema}}
            if profile.output_schema
            else {}
        ),
    )


async def ask(prompt: str, profile: Profile, cli_path: str | None = None) -> Reply:
    """Send one message and collect the reply.

    Never raises. A surface that dies on a bad turn is worse than one that says
    it failed, because the user is left wondering whether the message landed.
    """
    if not profile.enabled:
        return Reply(text="", error="That profile is not enabled.")

    profile.cwd.mkdir(parents=True, exist_ok=True)

    chunks: list[str] = []
    session_id: str | None = None
    cost: float | None = None
    structured: dict | None = None
    error: str | None = None

    try:
        # Drain the stream to completion rather than returning from inside it.
        # Returning early leaves the SDK's async generator suspended and closing
        # it then raises "aclose(): asynchronous generator is already running" -
        # once per call, on a path that runs on every moderation request.
        async for message in query(prompt=prompt, options=_options(profile, cli_path)):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
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
    except Exception as exc:  # noqa: BLE001 - surfaces must report, not crash
        log.exception("agent query failed (profile=%s)", profile.name)
        return Reply(text="".join(chunks).strip(), error=f"{type(exc).__name__}: {exc}")

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
