"""Guild mentions that aren't actions get a plain reply from the public
profile, capped per person, and never through the owner's lock or profile."""

import asyncio
import json
from pathlib import Path

import pytest

from quartermaster import agent
from quartermaster.config import Settings
from quartermaster.discord_ops import OpsPlan
from quartermaster.surfaces import discord_bot, moderation
from quartermaster.surfaces.discord_bot import HourlyQuota


def test_chat_is_a_plan_the_parser_can_emit():
    assert OpsPlan.from_json(json.dumps({"action": "chat"})).action == "chat"


def test_parser_is_told_to_prefer_chat_over_guessing():
    from quartermaster.discord_ops import PARSE_PROMPT

    assert "-> chat" in PARSE_PROMPT and "prefer chat over guessing" in PARSE_PROMPT


def test_quota_caps_per_person_per_rolling_hour():
    now = [0.0]
    quota = HourlyQuota(clock=lambda: now[0])
    assert all(quota.allow(1, 3) for _ in range(3))
    assert not quota.allow(1, 3)
    assert quota.allow(2, 3)  # someone else isn't affected
    now[0] = 3601
    assert quota.allow(1, 3)


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text, **_):
        self.sent.append(text)

    def typing(self):
        class _T:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return None
        return _T()


class FakeAuthor:
    id, display_name = 42, "Dave"


class FakeMessage:
    def __init__(self):
        self.channel, self.author = FakeChannel(), FakeAuthor()


@pytest.fixture
def bot(tmp_path: Path, monkeypatch):
    settings = Settings(vault=tmp_path / "Vault", discord_owner_id=1, prefs={"public": {"replies_per_hour": 2}})
    monkeypatch.setattr(discord_bot, "current_prefs", lambda s: s.prefs)
    return discord_bot.Quartermaster(settings)


def test_chat_is_answered_by_the_public_profile_with_the_sender_named(bot, monkeypatch):
    seen = []

    async def fake_ask(prompt, profile, cli, **_):
        seen.append((prompt, profile.name))
        return agent.Reply(text="They're fine, I guess.")

    monkeypatch.setattr(agent, "ask", fake_ask)
    msg = FakeMessage()
    asyncio.run(bot.answer_publicly(msg, "rate my build"))
    assert seen == [("Dave: rate my build", "public")]
    assert msg.channel.sent == ["They're fine, I guess."]
    assert not bot._busy.locked()


def test_over_the_cap_it_says_so_instead_of_calling_the_model(bot, monkeypatch):
    calls = []

    async def fake_ask(prompt, profile, cli, **_):
        calls.append(prompt)
        return agent.Reply(text="hi")

    monkeypatch.setattr(agent, "ask", fake_ask)
    msg = FakeMessage()
    for _ in range(3):
        asyncio.run(bot.answer_publicly(msg, "hey"))
    assert len(calls) == 2 and "enough for one hour" in msg.channel.sent[-1]


def test_moderation_hands_chat_back_to_the_caller(monkeypatch, tmp_path):
    async def fake_parse(settings, request):
        return OpsPlan(action="chat"), ""

    monkeypatch.setattr(moderation, "parse_request", fake_parse)
    monkeypatch.setattr(moderation.discord, "Member", FakeAuthor)
    msg = FakeMessage()
    msg.guild = object()
    msg.author = FakeAuthor()
    handled = asyncio.run(moderation.handle(Settings(vault=tmp_path), msg, None, "order me a pizza"))
    assert handled is False and msg.channel.sent == []


def test_a_non_owner_dm_is_ignored(bot):
    class DM(FakeMessage):
        mentions, guild = [], None

    msg = DM()
    msg.channel = discord_bot.discord.DMChannel.__new__(discord_bot.discord.DMChannel)
    assert bot._route(msg) is None
