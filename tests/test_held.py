"""DMs from the night chores are held and the bot delivers them at digest.hour."""

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from quartermaster.config import DEFAULTS, Settings
from quartermaster.discord_bot import bot as discord_bot, held, send


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    (tmp_path / "Vault" / "System").mkdir(parents=True)
    monkeypatch.setattr(held, "current_prefs", lambda s: DEFAULTS)  # digest.hour = 8
    return Settings(vault=tmp_path / "Vault", discord_owner_id=1, discord_bot_token="t")


def at(hour: int) -> datetime:
    return datetime(2026, 10, 4, hour, 0)


def test_night_is_midnight_until_the_digest_hour(settings):
    assert held.quiet(settings, at(3)) and held.quiet(settings, at(7))
    assert not held.quiet(settings, at(8)) and not held.quiet(settings, at(23))


def test_by_night_a_job_holds_by_day_it_sends(settings, monkeypatch):
    sent = []
    monkeypatch.setattr(send, "send_dm", lambda s, text: sent.append(text))
    monkeypatch.setattr(held, "quiet", lambda s, now=None: True)
    assert send.send_or_hold(settings, "questions", "reconcile") is True
    assert sent == [] and [i["text"] for i in held.load(settings)] == ["questions"]
    monkeypatch.setattr(held, "quiet", lambda s, now=None: False)
    assert send.send_or_hold(settings, "now", "reconcile") is False and sent == ["now"]


def test_nothing_is_due_before_morning_then_the_digest_goes_first(settings):
    held.hold(settings, "reconcile questions", "reconcile")
    held.hold(settings, "the digest", "digest")
    assert held.due(settings, at(3)) == []
    assert [i["what"] for i in held.due(settings, at(8))] == ["digest", "reconcile"]


def test_delivered_drops_only_that_one_and_the_file_goes_when_empty(settings):
    held.hold(settings, "a", "digest")
    held.hold(settings, "b", "reconcile")
    first, second = held.load(settings)
    held.delivered(settings, first["id"])
    assert held.load(settings) == [second]
    held.delivered(settings, second["id"])
    assert not held.held_path(settings).exists()


class Owner:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, text):
        if self.fail:
            raise RuntimeError("discord down")
        self.sent.append(text)


def test_the_bot_delivers_whats_due_and_keeps_what_failed(settings, monkeypatch):
    monkeypatch.setattr(held, "quiet", lambda s, now=None: False)
    held.hold(settings, "the digest", "digest")
    bot = discord_bot.Quartermaster(settings)

    owner = Owner(fail=True)
    monkeypatch.setattr(bot, "fetch_user", lambda uid: asyncio.sleep(0, owner))
    asyncio.run(bot._deliver_held())
    assert len(held.load(settings)) == 1  # retried next poll

    owner.fail = False
    asyncio.run(bot._deliver_held())
    assert owner.sent == ["the digest"] and held.load(settings) == []


def test_tidy_proposals_wait_for_morning_but_the_owners_dont():
    assert not discord_bot.offer_now({"source": "tidy"}, night=True)
    assert discord_bot.offer_now({"source": "tidy"}, night=False)
    assert discord_bot.offer_now({"source": "agent"}, night=True)
