"""Executing moderation plans in Discord.

The flow, in order, and the order is the point:

1. The model reads the request and emits a structured plan. It holds no tools.
2. Code resolves the target and checks the *invoker's* real permissions and
   role hierarchy. Nothing the model said can widen this.
3. Code gathers what would actually be affected and shows it.
4. A human confirms.
5. Code executes.

Steps 2 through 5 never consult the model again, so text posted in a channel
cannot influence what happens — the worst an injection achieves is a plan that
a person then declines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import discord

from .. import agent, discord_ops
from ..config import Settings
from ..discord_ops import OpsPlan

log = logging.getLogger(__name__)

CONFIRM_TIMEOUT = 120.0
TENOR_ENDPOINT = "https://tenor.googleapis.com/v2/search"
PREVIEW_SAMPLE = 5


class ConfirmView(discord.ui.View):
    """Approve or decline one plan.

    Only the person who asked may press, so a bystander cannot approve a
    destructive action on someone else's behalf. Timing out declines, because
    the safe default for an irreversible action is for it not to happen.
    """

    def __init__(self, invoker_id: int):
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self.invoker_id = invoker_id
        self.approved: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who asked can confirm this.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.approved = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.approved = False
        await interaction.response.defer()
        self.stop()


async def parse_request(settings: Settings, request: str) -> tuple[OpsPlan | None, str]:
    """Turn an English request into a plan. Returns (plan, error)."""
    profile = agent.parser_profile(settings, discord_ops.PLAN_SCHEMA)
    prompt = discord_ops.PARSE_PROMPT.format(request=request)

    reply = await agent.ask(prompt, profile, settings.claude_cli)
    if reply.error:
        return None, reply.error

    # Structured output arrives on the result, not as text. Fall back to parsing
    # text only if the schema path produced nothing.
    if reply.structured is not None:
        payload = json.dumps(reply.structured)
    elif reply.text.strip():
        payload = reply.text
    else:
        log.warning("parser returned neither structured output nor text")
        return None, "I couldn't work out what you wanted."

    try:
        return OpsPlan.from_json(payload), ""
    except (ValueError, KeyError) as exc:
        log.warning("unparseable plan: %s", payload[:300])
        return None, f"I couldn't turn that into an action ({exc})."


async def handle(
    settings: Settings,
    message: discord.Message,
    bot_user: discord.ClientUser,
    request: str,
) -> None:
    """Run one moderation request end to end, in a guild channel."""
    channel = message.channel
    guild = message.guild
    if guild is None or not isinstance(message.author, discord.Member):
        await channel.send("Moderation only works inside a server.")
        return

    async with channel.typing():
        plan, error = await parse_request(settings, request)
    if plan is None:
        await channel.send(f"⚠️ {error}")
        return

    bot_member = guild.me
    invoker_perms = channel.permissions_for(message.author)
    bot_perms = channel.permissions_for(bot_member)

    check = discord_ops.check_permissions(plan, invoker_perms, bot_perms)
    if not check.ok:
        await channel.send("❌ " + "\n".join(check.problems))
        return

    target: discord.Member | None = None
    if plan.is_member_action:
        target, candidates = discord_ops.resolve_member(plan.target_user or "", list(guild.members))
        if target is None:
            if candidates:
                names = ", ".join(f"`{m}`" for m in candidates[:6])
                await channel.send(f"❓ `{plan.target_user}` is ambiguous — did you mean: {names}?")
            else:
                await channel.send(f"❓ I couldn't find `{plan.target_user}` in this server.")
            return

        hierarchy = discord_ops.check_hierarchy(message.author, bot_member, target)
        if not hierarchy.ok:
            await channel.send("❌ " + "\n".join(hierarchy.problems))
            return

    # --- Preview -----------------------------------------------------------

    matched: list[discord.Message] = []
    replied_to: discord.Message | None = None

    # say and gif create a message rather than acting on one, so there is
    # nothing to match and "nothing matched" would be a false failure.
    needs_target = not plan.is_member_action and plan.action not in ("say", "gif")

    if needs_target:
        replied_to = await _replied_message(message)
        if replied_to is not None:
            # "delete that message" while replying to it is the most natural way
            # to ask, and it is completely unambiguous - Discord tells us exactly
            # which message you meant. Honour it instead of guessing from a
            # history scan, which is how "that" would otherwise become "the last
            # twenty".
            matched = [replied_to]
        else:
            matcher = discord_ops.build_matcher(plan, bot_user_id=bot_user.id)
            try:
                async for msg in channel.history(limit=max(plan.limit * 5, 100)):
                    if msg.id == message.id:
                        continue
                    if matcher(msg):
                        matched.append(msg)
                    if len(matched) >= plan.limit:
                        break
            except discord.Forbidden:
                await channel.send("❌ I can't read this channel's history.")
                return

        if not matched:
            await channel.send(
                "Nothing matched that. Nothing changed.\n"
                "_Tip: reply to a message and say \"delete that\" to target it exactly._"
            )
            return

    summary = _preview_text(plan, target, matched, targeted_reply=replied_to is not None)

    # Counting only reports; there is nothing to run.
    if plan.action == "count":
        await channel.send(summary)
        return

    # Only irreversible actions are worth interrupting for. Reacting, posting a
    # message or a gif adds to the channel rather than removing from it, and
    # undoing one is trivial — a confirmation step there is pure friction.
    if not plan.is_destructive:
        try:
            result = await _execute(plan, channel, target, matched, message.author)
        except discord.Forbidden as exc:
            result = f"❌ Discord refused: {exc.text or exc}"
        except discord.HTTPException as exc:
            result = f"❌ Discord error: {exc.text or exc}"

        # A bare "Sent." reads oddly next to the thing it just sent.
        if plan.action in ("say", "gif"):
            if result.startswith("❌"):
                await channel.send(result)
        else:
            await channel.send(result)
        return

    view = ConfirmView(message.author.id)
    prompt_msg = await channel.send(summary, view=view)
    await view.wait()

    if not view.approved:
        await prompt_msg.edit(
            content=summary + "\n\n**Cancelled.** Nothing changed.", view=None
        )
        return

    # --- Execute -----------------------------------------------------------

    try:
        result = await _execute(plan, channel, target, matched, message.author)
    except discord.Forbidden as exc:
        result = f"❌ Discord refused: {exc.text or exc}"
    except discord.HTTPException as exc:
        result = f"❌ Discord error: {exc.text or exc}"

    await prompt_msg.edit(content=summary + f"\n\n{result}", view=None)


def _tenor_key() -> str | None:
    return os.getenv("TENOR_API_KEY") or None


async def _find_gif(query: str) -> str | None:
    """First Tenor result for a query, or None.

    Optional by design: no key just means gif search is unavailable, not that
    the bot fails to start.
    """
    key = _tenor_key()
    if not key or not query.strip():
        return None

    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                TENOR_ENDPOINT,
                params={
                    "q": query, "key": key, "limit": 1,
                    "media_filter": "gif", "contentfilter": "medium",
                },
            )
            resp.raise_for_status()
            results = resp.json().get("results") or []
            if not results:
                return None
            return results[0]["media_formats"]["gif"]["url"]
    except Exception:  # noqa: BLE001 - a failed search is not a crash
        log.exception("tenor search failed")
        return None


async def _replied_message(message: discord.Message) -> discord.Message | None:
    """The message this one is a reply to, if any.

    Discord resolves the reference for us most of the time; when it doesn't
    (the referenced message wasn't in the cache) fetch it. A DeletedReferencedMessage
    means the target is already gone, so there is nothing to act on.
    """
    ref = message.reference
    if ref is None:
        return None

    resolved = getattr(ref, "resolved", None)
    if isinstance(resolved, discord.Message):
        return resolved
    if isinstance(resolved, discord.DeletedReferencedMessage):
        return None

    if ref.message_id is None:
        return None
    try:
        return await message.channel.fetch_message(ref.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def _preview_text(
    plan: OpsPlan,
    target: Any,
    matched: list[discord.Message],
    *,
    targeted_reply: bool = False,
) -> str:
    # When a reply pinned the target exactly, saying "up to 20" is a lie about
    # what will happen. What gets approved has to match what runs.
    if targeted_reply:
        lines = [f"**Plan:** **{plan.action}** the message you replied to"]
    else:
        lines = [f"**Plan:** {plan.describe()}"]

    if plan.is_member_action and target is not None:
        lines.append(f"**Target:** {target} (`{target.id}`)")
    else:
        lines.append(f"**Matched:** {len(matched)} message(s)")
        for msg in matched[:PREVIEW_SAMPLE]:
            excerpt = (msg.content or "").replace("\n", " ")[:80] or "(no text)"
            lines.append(f"  • `{msg.author}`: {excerpt}")
        if len(matched) > PREVIEW_SAMPLE:
            lines.append(f"  • …and {len(matched) - PREVIEW_SAMPLE} more")

        too_old = sum(1 for m in matched if discord_ops.too_old_for_bulk(m))
        if too_old:
            lines.append(
                f"_{too_old} are over 14 days old and must be deleted one at a "
                "time, which is slower._"
            )

    if plan.is_destructive:
        lines.append("\n**This cannot be undone.** Confirm to proceed.")
    return "\n".join(lines)


async def _execute(
    plan: OpsPlan,
    channel: Any,
    target: Any,
    matched: list[discord.Message],
    invoker: Any,
) -> str:
    """Perform the approved plan. No model involvement past this point."""
    reason = f"Quartermaster, requested by {invoker} — {plan.reason or 'no reason given'}"[:500]

    if plan.action in ("react", "unreact"):
        if not matched:
            return "❌ Reply to the message you want me to react to."
        if not plan.emoji:
            return "❌ I couldn't tell which emoji you meant."
        try:
            if plan.action == "react":
                await matched[0].add_reaction(plan.emoji)
                return f"✅ Reacted with {plan.emoji}."
            await matched[0].clear_reaction(plan.emoji)
            return f"✅ Removed {plan.emoji}."
        except discord.HTTPException:
            # Almost always an emoji from a server the bot isn't in.
            return f"❌ I can't use {plan.emoji} — custom emoji only work in servers I'm in."

    if plan.action == "say":
        if not plan.text:
            return "❌ Nothing to say."
        await channel.send(plan.text)
        return "✅ Sent."

    if plan.action == "gif":
        url = await _find_gif(plan.query or "")
        if url is None:
            return (
                "❌ GIF search needs a Tenor key. Add `TENOR_API_KEY` to .env "
                "(free at developers.google.com/tenor) and restart me."
                if not _tenor_key()
                else f"❌ Nothing found for `{plan.query}`."
            )
        await channel.send(url)
        return "✅ Sent."

    if plan.action == "delete":
        deleted = 0
        for msg in matched:
            try:
                await msg.delete()
                deleted += 1
            except discord.NotFound:
                pass  # already gone; not a failure
        return f"✅ Deleted {deleted} message(s)."

    if plan.action in ("pin", "unpin"):
        done = 0
        for msg in matched:
            await (msg.pin(reason=reason) if plan.action == "pin" else msg.unpin(reason=reason))
            done += 1
        return f"✅ {plan.action.capitalize()}ned {done} message(s)."

    if plan.action == "kick":
        await target.kick(reason=reason)
        return f"✅ Kicked {target}."

    if plan.action == "ban":
        await target.ban(reason=reason, delete_message_days=plan.ban_delete_days)
        extra = f", deleting {plan.ban_delete_days} day(s) of messages" if plan.ban_delete_days else ""
        return f"✅ Banned {target}{extra}."

    if plan.action == "unban":
        await channel.guild.unban(target, reason=reason)
        return f"✅ Unbanned {target}."

    if plan.action == "timeout":
        from datetime import timedelta

        await target.timeout(timedelta(minutes=plan.timeout_minutes or 10), reason=reason)
        return f"✅ Timed out {target} for {plan.timeout_minutes or 10} min."

    if plan.action == "untimeout":
        await target.timeout(None, reason=reason)
        return f"✅ Removed timeout from {target}."

    if plan.action in ("voice_mute", "voice_unmute", "voice_deafen", "voice_undeafen"):
        if target.voice is None:
            return f"❌ {target} isn't in a voice channel, so there's nothing to change."

        on = plan.action in ("voice_mute", "voice_deafen")
        field = "mute" if "mute" in plan.action else "deafen"
        await target.edit(**{field: on}, reason=reason)

        verb = f"{'' if on else 'un'}{field}d"
        if on and plan.duration_seconds:
            asyncio.create_task(_undo_after(target, field, plan.duration_seconds, reason))
            # Stated plainly: this timer lives in memory only.
            return (
                f"✅ Voice {verb} {target} for {plan.duration_seconds}s. "
                "_The undo is scheduled in memory — if I restart before then, "
                "they stay that way._"
            )
        return f"✅ Voice {verb} {target}."

    if plan.action == "disconnect":
        if target.voice is None:
            return f"❌ {target} isn't in a voice channel."
        await target.move_to(None, reason=reason)
        return f"✅ Disconnected {target} from voice."

    return "Nothing to do."


async def _undo_after(target: Any, field: str, seconds: int, reason: str) -> None:
    """Reverse a voice mute/deafen after a delay.

    Discord voice mutes have no duration of their own, so "mute them for 30
    seconds" has to be two calls with a wait between. Deliberately best-effort:
    the timer is in memory, and the caller tells the user that.
    """
    try:
        await asyncio.sleep(seconds)
        await target.edit(**{field: False}, reason=f"{reason} (auto-undo after {seconds}s)")
    except Exception:  # noqa: BLE001 - a failed undo must not kill the bot
        log.exception("failed to undo voice %s on %s", field, target)
