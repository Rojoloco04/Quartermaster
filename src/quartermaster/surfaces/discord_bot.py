"""The Discord surface.

The bot is expected to sit in a normal server alongside other people, so who is
talking decides which profile answers — and the profile, not a prompt rule,
decides what can be reached. See ``agent.Profile``.

Today only the owner profile is enabled, and only in DMs. The public profile
exists, is wired in, and is switched off; turning it on means granting
capabilities to an empty list rather than removing access from a privileged
agent, which is the only ordering that fails safe.

The private one-person server is no longer required — Discord only needs the bot
to share *some* guild with you before it can DM. Your normal server does that.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import PurePath
from urllib.parse import urlparse

import discord

from .. import agent
from . import moderation
from ..config import Settings, load_settings

log = logging.getLogger(__name__)

CHUNK = 1900  # Discord's ceiling is 2000; leave room for fence repair.


def split_message(text: str, limit: int = CHUNK) -> list[str]:
    """Split a reply to fit Discord's message ceiling.

    Splits on paragraph, then line, then hard-wraps. Code fences are closed and
    reopened across the boundary, otherwise the second half renders as prose and
    the formatting is lost exactly when the content is most structured.
    """
    if len(text) <= limit:
        return [text] if text else []

    parts: list[str] = []
    remaining = text

    while len(remaining) > limit:
        window = remaining[:limit]
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx > limit // 2:
                cut = idx + len(sep)
                break
        else:
            cut = limit

        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        parts.append(remaining)

    return _repair_code_fences(parts)


def _repair_code_fences(parts: list[str]) -> list[str]:
    out: list[str] = []
    carry_lang: str | None = None

    for part in parts:
        if carry_lang is not None:
            part = f"```{carry_lang}\n{part}"

        fences = [ln for ln in part.splitlines() if ln.lstrip().startswith("```")]
        if len(fences) % 2 == 1:
            carry_lang = fences[-1].lstrip()[3:].strip()
            part = part + "\n```"
        else:
            carry_lang = None

        out.append(part)

    return out


class Quartermaster(discord.Client):
    def __init__(self, settings: Settings, *, with_members: bool = True):
        intents = discord.Intents.default()
        # Privileged, and must be enabled in the Developer Portal too. Without
        # it every message arrives with empty content and the bot silently
        # ignores everything - the most common way this setup fails.
        intents.message_content = True
        intents.dm_messages = True
        # Also privileged. Needed to resolve "ban dave" to a member and to read
        # role positions for the hierarchy check. Optional: without it the bot
        # still runs and the vault assistant works, moderation just turns off.
        intents.members = with_members

        # Nothing the bot sends pings anyone unless a call opts in (moderation's
        # `say`). A model reply, or a preview quoting someone's message, must
        # never become an @everyone.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.moderation_enabled = with_members
        self.owner = agent.owner_profile(settings)
        self.public = agent.public_profile(settings)
        self._busy = asyncio.Lock()

    async def on_ready(self) -> None:
        # Logging rather than print: print() is block-buffered when stdout is a
        # pipe, so the whole startup banner vanished when run in the background
        # and the "moderation is off" warning never reached the log.
        log.info("online as %s", self.user)
        log.info("  vault:       %s", self.settings.vault)
        log.info("  transcripts: %s", agent.transcript_dir(self.settings.vault))
        log.info("  owner id:    %s", self.settings.discord_owner_id)
        log.info("  public mode: %s", "on" if self.public.enabled else "off")
        if self.moderation_enabled:
            log.info("  moderation:  on (gated by each invoker's own permissions)")
        else:
            log.warning("  moderation:  OFF - SERVER MEMBERS INTENT is not enabled")

    def _route(self, message: discord.Message) -> str | None:
        """Decide which surface handles this message, if any.

        Returning None means stay silent. Silence is the default for everything
        that is not an explicit, recognised case.
        """
        is_owner = message.author.id == self.settings.discord_owner_id
        is_dm = isinstance(message.channel, discord.DMChannel)
        mentioned = self.user is not None and self.user in message.mentions

        # Vault access is DM-only. In a shared channel the replies would be
        # readable by everyone present, so even the owner does not get it there.
        if is_owner and is_dm:
            return "owner"

        # Moderation in a guild channel. Anyone may ask; whether anything
        # happens is decided by their real Discord permissions, checked in code.
        if mentioned and message.guild is not None:
            return "moderation" if self.moderation_enabled else "moderation_off"

        if mentioned and self.public.enabled:
            return "public"

        return None

    async def on_message(self, message: discord.Message) -> None:
        if self.user is not None and message.author.id == self.user.id:
            return

        route = self._route(message)
        if route is None:
            return

        prompt = message.content
        if self.user is not None:
            prompt = prompt.replace(f"<@{self.user.id}>", "").strip()

        if message.attachments:
            listing = "\n".join(f"- {a.filename}: {a.url}" for a in message.attachments)
            prompt = f"{prompt}\n\nAttachments:\n{listing}".strip()

        if not prompt:
            return

        if route == "moderation_off":
            # Never ignore a mention in silence: silence is indistinguishable
            # from being broken, which is exactly how this failed the first time
            # someone used it.
            await message.channel.send(
                "I can't moderate yet — **SERVER MEMBERS INTENT** is off, so I can't "
                "resolve names or read roles.\n"
                "Enable it at discord.com/developers/applications → your app → Bot "
                "→ Privileged Gateway Intents, then restart me."
            )
            return

        if route == "moderation":
            # Not under the busy lock: moderation waits on a human pressing a
            # button, and holding the lock for two minutes would block the
            # owner's DM thread behind it.
            await moderation.handle(self.settings, message, self.user, prompt)
            return

        profile = self.owner if route == "owner" else self.public

        # One turn at a time. Two concurrent turns would both resume the same
        # session and interleave, corrupting the shared thread.
        if self._busy.locked():
            await message.channel.send("Still working on the last one - one sec.")
            return

        async with self._busy, message.channel.typing():
            status = LiveStatus(message.channel)
            await status.start()
            try:
                reply = await agent.ask(prompt, profile, self.settings.claude_cli, on_progress=status.update)
            finally:
                await status.done()

        if reply.error:
            # Say so rather than leaving a silence that looks like being ignored.
            await message.channel.send(f"⚠️ {reply.error}")
        elif not status.sent_text:
            await message.channel.send(
                "(I finished but produced no reply. That's a bug - tell me what you asked.)"
            )


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


class LiveStatus:
    """A status line kept at the bottom of the conversation while a turn runs.

    Reply text is sent the moment the agent writes it - an intermediate "let me
    check" arrives as its own message - and the status line is re-posted
    beneath it, then deleted when the turn ends. Edits are throttled because
    Discord rate-limits them (about 5 per 5 seconds per channel).
    """

    THINKING = "💭 Thinking…"
    MIN_EDIT_GAP = 1.2

    def __init__(self, channel: discord.abc.Messageable):
        self.channel = channel
        self.message: discord.Message | None = None
        self.sent_text = False
        self._last_edit = 0.0

    async def start(self) -> None:
        self.message = await self.channel.send(self.THINKING)
        self._last_edit = time.monotonic()

    async def update(self, kind: str, payload: object) -> None:
        if kind == "text":
            text = str(payload).strip()
            if not text:
                return
            await self._drop()
            for part in split_message(text):
                await self.channel.send(part)
            self.sent_text = True
            await self.start()
        elif kind == "tool" and self.message is not None:
            now = time.monotonic()
            if now - self._last_edit >= self.MIN_EDIT_GAP:
                name, tool_input = payload  # type: ignore[misc]
                self._last_edit = now
                await self.message.edit(content=f"🔧 {describe_tool(name, tool_input or {})}…")

    async def done(self) -> None:
        await self._drop()

    async def _drop(self) -> None:
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass  # already gone; nothing to tidy
            self.message = None


def run(settings: Settings | None = None) -> int:
    settings = settings or load_settings()
    settings.require("discord_bot_token", "discord_owner_id")

    if not settings.vault.exists():
        log.error("No vault at %s. Run 'qm init' first.", settings.vault)
        return 1
    log.info("logging to %s", settings.log_path)  # configured by cli.main

    # Try with moderation, then without. A feature nobody has switched on in the
    # portal yet must not take down the assistant that gets used every day.
    for with_members in (True, False):
        client = Quartermaster(settings, with_members=with_members)
        try:
            client.run(settings.discord_bot_token or "", log_handler=None)
            return 0
        except discord.PrivilegedIntentsRequired:
            if with_members:
                log.warning(
                    "SERVER MEMBERS INTENT is off, so moderation is unavailable. Starting "
                    "without it - DMs still work. Enable it at discord.com/developers/"
                    "applications -> your app -> Bot -> Privileged Gateway Intents."
                )
                continue
            log.error(
                "Discord refused the connection: MESSAGE CONTENT INTENT is off. Enable it "
                "at discord.com/developers/applications -> your app -> Bot -> Privileged "
                "Gateway Intents. Without it every message arrives empty."
            )
            return 1
        except discord.LoginFailure:
            log.error("Discord refused the token. Check DISCORD_BOT_TOKEN in .env.")
            return 1
        except KeyboardInterrupt:
            return 0

    return 1
