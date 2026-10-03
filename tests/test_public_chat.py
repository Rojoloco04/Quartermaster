"""Guild mentions that aren't actions get a plain reply from the public
profile, capped per person, and never through the owner's lock or profile."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quartermaster import agent
from quartermaster.config import Settings
from quartermaster.discord_bot.plans import OpsPlan
from quartermaster.discord_bot import bot as discord_bot, moderation
from quartermaster.discord_bot.bot import HourlyQuota


def test_chat_is_a_plan_the_parser_can_emit():
    assert OpsPlan.from_json(json.dumps({"action": "chat"})).action == "chat"


def test_parser_is_told_to_prefer_chat_over_guessing():
    from quartermaster.discord_bot.plans import PARSE_PROMPT

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
    def __init__(self, history=()):
        self.sent = []
        self._history = list(history)  # newest first, as Discord returns it
        self.history_calls = []

    def history(self, **kwargs):
        self.history_calls.append(kwargs)
        found = self._history[: kwargs["limit"]]

        async def gen():
            for m in found:
                yield m
        return gen()

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
    def __init__(self, history=()):
        self.channel, self.author = FakeChannel(history), FakeAuthor()
        self.created_at = datetime(2026, 9, 30, 23, 4, tzinfo=timezone.utc)


class Person:
    def __init__(self, id, display_name):
        self.id, self.display_name = id, display_name


class Said:
    """A message in the channel's history."""

    def __init__(self, author, content, reference=None, attachments=(), embeds=()):
        self.author, self.clean_content = author, content
        self.reference, self.attachments, self.embeds = reference, list(attachments), list(embeds)


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


def test_the_channels_recent_messages_come_first_oldest_first(bot, monkeypatch):
    phlabry, me = Person(7, "Phlabry"), Person(99, "QM")
    monkeypatch.setattr(discord_bot.Quartermaster, "user", me, raising=False)
    seen = []

    async def fake_ask(prompt, profile, cli, **_):
        seen.append(prompt)
        return agent.Reply(text="fire, honestly")

    monkeypatch.setattr(agent, "ask", fake_ask)
    msg = FakeMessage(history=[  # newest first
        Said(me, "need more context - who's \"he\"?"),
        Said(phlabry, "and my knot thick"),
        Said(phlabry, "yeah my glock sick"),
    ])
    asyncio.run(bot.answer_publicly(msg, "phlabry with his bars"))
    prompt = seen[0]
    assert prompt.index("Phlabry: yeah my glock sick") < prompt.index("Phlabry: and my knot thick")
    assert "Quartermaster (you): need more context" in prompt
    assert prompt.rstrip().endswith("Dave: phlabry with his bars")
    assert msg.channel.history_calls == [{"limit": 25, "before": msg}]  # no time window


def test_context_trims_long_lines_and_drops_the_oldest_past_the_cap():
    someone = Person(7, "Sam")
    newest_first = [Said(someone, f"line {i} " + "x" * 1000) for i in range(30)]
    msg = FakeMessage(history=newest_first)
    text = asyncio.run(discord_bot.channel_context(msg, None, 30))
    lines = text.splitlines()
    assert all(len(line) <= discord_bot.CONTEXT_LINE_CHARS + 20 for line in lines)
    assert len(text) <= discord_bot.CONTEXT_TOTAL_CHARS
    assert lines[-1].startswith("Sam: line 0 ")  # the newest survives


def test_context_labels_replies_and_bare_attachments():
    sam, dave = Person(7, "Sam"), Person(8, "Dave")
    ref = type("Ref", (), {})()
    ref.resolved = discord_bot.discord.Message.__new__(discord_bot.discord.Message)
    ref.resolved.author = dave  # type: ignore[misc]
    msg = FakeMessage(history=[Said(sam, "", attachments=["pic.png"]), Said(sam, "lol", reference=ref)])
    text = asyncio.run(discord_bot.channel_context(msg, None, 10))
    assert text.splitlines() == ["Sam (replying to Dave): lol", "Sam: [attachment]"]


def test_context_off_or_unreadable_is_just_no_context():
    assert asyncio.run(discord_bot.channel_context(FakeMessage(), None, 0)) == ""

    class Forbidden(FakeChannel):
        def history(self, **_):
            raise discord_bot.discord.Forbidden(type("R", (), {"status": 403, "reason": "no"})(), "Missing Access")

    msg = FakeMessage()
    msg.channel = Forbidden()
    assert asyncio.run(discord_bot.channel_context(msg, None, 20)) == ""


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
