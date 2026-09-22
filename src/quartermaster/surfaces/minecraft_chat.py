"""The Minecraft server from a guild channel: "@Quartermaster start the mc
server", "who's on?", "whitelist Steve".

Same shape as moderation: the parser model turns the request into an OpsPlan
(action "minecraft") and code does the rest. Who may do what is decided here,
never by the model:

- status, link, verify: anyone in the server.
- start, stop, command: the owner, or a member whose linked Minecraft name is
  on the server's op list (``minecraft.may_control``). Commands still go
  through ``ALLOWED_COMMANDS``, for ops too.
- stop asks the invoker to confirm: it kicks everyone who's on.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from ..config import Settings
from ..discord_ops import OpsPlan
from ..integrations import minecraft

log = logging.getLogger(__name__)

# Pending link codes live as long as the bot process.
codes = minecraft.LinkCodes()


async def _call(fn, *args) -> str:
    """Run a blocking server call (RCON, a JVM start, a 60s stop) off the loop."""
    try:
        return await asyncio.to_thread(fn, *args)
    except minecraft.MinecraftError as exc:
        return f"❌ {exc}"


async def handle(settings: Settings, message: discord.Message, plan: OpsPlan) -> None:
    channel, invoker = message.channel, message.author
    op, arg = plan.minecraft_op, plan.minecraft_arg or ""
    log.info("[mc] %s (%s) asked: %s %s in #%s", invoker, invoker.id, op, arg, channel)

    if op == "status":
        await channel.send(await _call(minecraft.status, settings))
        return

    if op == "link":
        await channel.send(await _link(settings, invoker, arg))
        return

    if op == "verify":
        name = codes.redeem(invoker.id, arg)
        if name is None:
            await channel.send("❌ That code isn't right or has expired. Ask me to link you again for a new one.")
            return
        await asyncio.to_thread(minecraft.add_link, minecraft.links_path(settings), invoker.id, name)
        log.info("[mc] linked %s (%s) to %s", invoker, invoker.id, name)
        ok, _ = minecraft.may_control(settings, invoker.id)
        extra = " You're an op, so you can start, stop and run commands." if ok else " You can check who's on; ops can do more."
        await channel.send(f"✅ Linked to **{name}**.{extra}")
        return

    ok, why = minecraft.may_control(settings, invoker.id)
    if not ok:
        log.warning("[mc] denied: %s (%s) tried %s %s", invoker, invoker.id, op, arg)
        await channel.send(f"❌ {why}")
        return

    if op == "start":
        async with channel.typing():
            await channel.send(await _call(minecraft.start, settings))
    elif op == "stop":
        await _stop(settings, message)
    elif op == "command":
        await channel.send(await _call(minecraft.command, settings, arg))


async def _link(settings: Settings, invoker, name: str) -> str:
    if not minecraft.NAME.fullmatch(name):
        return "❌ Tell me your exact Minecraft name, e.g. \"link me to Steve\"."
    online = await _call(minecraft.rcon, settings, "list")
    if online.startswith("❌"):
        return "❌ The server has to be running, with you on it, to link: I whisper you a code in-game."
    match = next((p for p in minecraft.online_players(online) if p.lower() == name.lower()), None)
    if match is None:
        return f"❌ {name} isn't on the server right now. Join, then ask again: I whisper you a code in-game."
    code = codes.issue(invoker.id, match)
    sent = await _call(minecraft.rcon, settings,
                       f"tell {match} Quartermaster: to link Discord user {invoker.name}, mention the bot "
                       f"in Discord with \"verify {code}\" within 10 minutes. Not you? Ignore this.")
    if sent.startswith("❌"):
        return sent
    return f"I've whispered a code to **{match}** in-game. Mention me with \"verify <code>\" within 10 minutes."


async def _stop(settings: Settings, message: discord.Message) -> None:
    from .moderation import ConfirmView  # moderation imports this module

    who = await _call(minecraft.status, settings)
    view = ConfirmView(message.author.id)
    prompt = await message.channel.send(f"**Stop the Minecraft server?** {who}", view=view)
    await view.wait()
    if not view.approved:
        await prompt.edit(content=f"**Stop the Minecraft server?** {who}\n\n**Cancelled.**", view=None)
        return
    await prompt.edit(content=f"**Stop the Minecraft server?** {who}\n\nStopping…", view=None)
    result = await _call(minecraft.stop, settings)
    log.info("[mc] %s (%s) stopped the server: %s", message.author, message.author.id, result)
    await prompt.edit(content=f"**Stop the Minecraft server?** {who}\n\n{result}", view=None)
