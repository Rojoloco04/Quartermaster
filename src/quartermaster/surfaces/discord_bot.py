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

        super().__init__(intents=intents)
        self.settings = settings
        self.moderation_enabled = with_members
        self.owner = agent.owner_profile(settings)
        self.public = agent.public_profile(settings)
        self._busy = asyncio.Lock()

    async def on_ready(self) -> None:
        log.info("connected as %s", self.user)
        print(f"\nQuartermaster online as {self.user}")
        print(f"  vault:       {self.settings.vault}")
        print(f"  transcripts: {agent.transcript_dir(self.settings.vault)}")
        print(f"  owner id:    {self.settings.discord_owner_id}")
        print(f"  public mode: {'on' if self.public.enabled else 'off'}")
        if self.moderation_enabled:
            print("  moderation:  on (gated by each invoker's own Discord permissions)")
        else:
            print("  moderation:  OFF - SERVER MEMBERS INTENT is not enabled")
        print("\nDM the bot to talk to it. Ctrl-C to stop.\n")

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
        if mentioned and message.guild is not None and self.moderation_enabled:
            return "moderation"

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
            reply = await agent.ask(prompt, profile, self.settings.claude_cli)

        await self._send(message.channel, reply)

    async def _send(self, channel: discord.abc.Messageable, reply: agent.Reply) -> None:
        if reply.text:
            for part in split_message(reply.text):
                await channel.send(part)

        if reply.error:
            # Say so rather than leaving a silence that looks like being ignored.
            await channel.send(f"⚠️ {reply.error}")
        elif not reply.text:
            await channel.send(
                "(I finished but produced no reply. That's a bug - tell me what you asked.)"
            )


def run(settings: Settings | None = None) -> int:
    settings = settings or load_settings()
    settings.require("discord_bot_token", "discord_owner_id")

    if not settings.vault.exists():
        print(f"No vault at {settings.vault}. Run 'qm init' first.")
        return 1

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    # Try with moderation, then without. A feature nobody has switched on in the
    # portal yet must not take down the assistant that gets used every day.
    for with_members in (True, False):
        client = Quartermaster(settings, with_members=with_members)
        try:
            client.run(settings.discord_bot_token or "", log_handler=None)
            return 0
        except discord.PrivilegedIntentsRequired:
            if with_members:
                print(
                    "\nSERVER MEMBERS INTENT is off, so moderation is unavailable.\n"
                    "Starting without it - DMs and the vault assistant still work.\n\n"
                    "To enable moderation: discord.com/developers/applications ->\n"
                    "your app -> Bot -> Privileged Gateway Intents -> Server Members\n"
                    "Intent -> Save Changes, then restart.\n"
                )
                continue

            print(
                "\nDiscord refused the connection: MESSAGE CONTENT INTENT is off.\n"
                "Enable it at discord.com/developers/applications -> your app -> Bot\n"
                "-> Privileged Gateway Intents -> Message Content Intent -> Save Changes.\n"
                "Without it your messages arrive empty and the bot ignores everything."
            )
            return 1
        except discord.LoginFailure:
            print("\nDiscord refused the token. Check DISCORD_BOT_TOKEN in .env.")
            return 1
        except KeyboardInterrupt:
            return 0

    return 1
