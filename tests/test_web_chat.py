"""The web chat: owner turns from `qm web`, streamed, sharing the Discord session."""

import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from quartermaster import agent
from quartermaster.config import DEFAULTS, Settings
from quartermaster.surfaces import chat, web
from quartermaster.surfaces.discord_bot import Quartermaster

from test_bot_commands import FakeChannel


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setattr(Settings, "log_path", property(lambda self: tmp_path / "logs" / "quartermaster.log"))
    return Settings(vault=tmp_path / "Vault", prefs=DEFAULTS, discord_owner_id=1)


def client(settings):
    c = TestClient(web.build_app(settings), base_url="http://127.0.0.1")
    page = c.get("/chat").text
    csrf = page.split('name="qm-csrf" content="')[1].split('"')[0]
    return c, csrf


def say(c, csrf, text):
    r = c.post("/api/chat", json={"text": text}, headers={"X-QM-CSRF": csrf})
    assert r.status_code == 200, r.text
    return [json.loads(line[6:]) for line in r.text.split("\n\n") if line.startswith("data: ")]


def test_a_turn_streams_text_and_tools(settings, monkeypatch):
    seen = []

    async def fake_ask(prompt, profile, cli, on_progress=None):
        seen.append(profile)
        await on_progress("tool", ("mcp__google__list_events", {}))
        await on_progress("text", "You have **one** thing today.")
        return agent.Reply(text="done")

    monkeypatch.setattr(agent, "ask", fake_ask)
    c, csrf = client(settings)
    events = say(c, csrf, "what's on today")
    assert [e["kind"] for e in events] == ["start", "tool", "text"]
    assert events[1]["text"] == "Checking your calendar"
    assert "<strong>one</strong>" in events[2]["html"]
    assert seen[0].name == "owner" and seen[0].share_session
    assert chat.TurnLock(chat.lock_path(settings)).acquire()  # released after the turn


def test_errors_and_silence_are_reported(settings, monkeypatch):
    async def failing(prompt, profile, cli, on_progress=None):
        return agent.Reply(text="", error="spend cap reached")

    monkeypatch.setattr(agent, "ask", failing)
    c, csrf = client(settings)
    assert say(c, csrf, "hi")[-1] == {"kind": "error", "text": "spend cap reached"}

    async def silent(prompt, profile, cli, on_progress=None):
        return agent.Reply(text="")

    monkeypatch.setattr(agent, "ask", silent)
    assert say(c, csrf, "hi")[-1]["kind"] == "error"


def test_requires_the_page_token(settings):
    c, _ = client(settings)
    assert c.post("/api/chat", json={"text": "hi"}).status_code == 403
    assert c.post("/api/chat", json={"text": "hi"}, headers={"X-QM-CSRF": "nope"}).status_code == 403


def test_start_fresh_makes_one_turn_skip_the_shared_session(settings, monkeypatch):
    seen = []

    async def fake_ask(prompt, profile, cli, on_progress=None):
        seen.append(profile.share_session)
        await on_progress("text", "ok")
        return agent.Reply(text="ok")

    monkeypatch.setattr(agent, "ask", fake_ask)
    c, csrf = client(settings)
    assert say(c, csrf, "start fresh") == [{"kind": "note", "text": chat.FRESH_NOTE}]
    say(c, csrf, "one")
    say(c, csrf, "two")
    assert seen == [False, True]


def test_stop_with_nothing_running(settings):
    c, csrf = client(settings)
    assert say(c, csrf, "stop") == [{"kind": "note", "text": "Nothing is running here."}]


def test_busy_while_discord_holds_the_turn(settings, monkeypatch):
    called = []
    monkeypatch.setattr(agent, "ask", lambda *a, **k: called.append(1))
    held = chat.TurnLock(chat.lock_path(settings))
    assert held.acquire()
    try:
        c, csrf = client(settings)
        assert "Busy with a message from Discord" in say(c, csrf, "hi")[0]["text"]
        assert not called
    finally:
        held.release()


def test_turn_lock_is_exclusive_and_released(tmp_path):
    a, b = chat.TurnLock(tmp_path / "t.lock"), chat.TurnLock(tmp_path / "t.lock")
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


async def test_discord_says_busy_while_the_web_chat_holds_the_turn(tmp_path, monkeypatch):
    called = []

    async def fake_ask(*a, **k):
        called.append(1)

    monkeypatch.setattr(agent, "ask", fake_ask)
    qm, ch = Quartermaster(Settings(vault=tmp_path, discord_owner_id=1)), FakeChannel()
    held = chat.TurnLock(chat.lock_path(qm.settings))
    assert held.acquire()
    try:
        await qm.run_turn(ch, "hi", qm.owner)
    finally:
        held.release()
    assert ch.sent == ["Busy with a message from the web chat. Try again when it's answered."]
    assert not called


def test_session_control_words():
    assert chat.session_control("Stop.") == "stop"
    assert chat.session_control("start fresh") == "new"
    assert chat.session_control("stop reminding me about X") is None


def test_quiet_for_the_limit_starts_a_fresh_session(tmp_path, monkeypatch):
    import os
    import time as _time
    monkeypatch.setattr(agent.Path, "home", lambda: tmp_path / "home")
    s = Settings(vault=tmp_path / "Vault", prefs={"chat": {"fresh_after_minutes": 5}})
    owner = agent.owner_profile(s)
    assert chat.continue_or_fresh(s, owner).share_session  # no sessions yet
    folder = agent.transcript_dir(s.vault)
    folder.mkdir(parents=True)
    session = folder / "one.jsonl"
    session.write_text("{}")
    now = _time.time()
    os.utime(session, (now - 4 * 60, now - 4 * 60))
    assert chat.continue_or_fresh(s, owner, now).share_session
    os.utime(session, (now - 6 * 60, now - 6 * 60))
    assert not chat.continue_or_fresh(s, owner, now).share_session
    off = Settings(vault=s.vault, prefs={"chat": {"fresh_after_minutes": 0}})
    assert chat.continue_or_fresh(off, owner, now).share_session
