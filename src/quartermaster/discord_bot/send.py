"""One-shot Discord DM sending, for jobs that run outside the bot's live
gateway connection - a scheduled task, most notably.

Separate from ``discord_bot.bot.Quartermaster`` (the long-lived gateway
client) on purpose: a scheduled task is a fresh process every time, with no
running gateway connection to reuse, and sending a DM only needs the REST
API - ``login()`` authenticates over HTTP without opening a websocket, which
is exactly the difference between it and ``connect()``/``start()``.
"""

from __future__ import annotations

import asyncio

import discord

from ..config import Settings
from . import held
from .bot import split_message


async def _send(settings: Settings, text: str) -> None:
    client = discord.Client(intents=discord.Intents.none())
    await client.login(settings.discord_bot_token or "")
    try:
        user = await client.fetch_user(settings.discord_owner_id)
        for part in split_message(text):
            await user.send(part)
    finally:
        await client.close()


def send_dm(settings: Settings, text: str) -> None:
    """Send ``text`` to the owner's DMs, chunked to fit Discord's message limit."""
    settings.require("discord_bot_token", "discord_owner_id")
    if not text:
        return
    asyncio.run(_send(settings, text))


def send_or_hold(settings: Settings, text: str, what: str) -> bool:
    """A job's DM: sent now by day, held for the bot to deliver at
    ``digest.hour`` by night (see ``held``). True if it was held."""
    if not text:
        return False
    if held.quiet(settings):
        held.hold(settings, text, what)
        return True
    send_dm(settings, text)
    return False
