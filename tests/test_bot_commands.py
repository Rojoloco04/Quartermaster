"""Owner DM commands: !stop cancels the running turn, !new starts a fresh thread."""

import asyncio
from pathlib import Path

from quartermaster import agent
from quartermaster.config import Settings
from quartermaster.surfaces.discord_bot import Quartermaster


class FakeMessage:
    def __init__(self, content):
        self.content = content

    async def edit(self, content):
        self.content = content

    async def delete(self):
        pass


class FakeChannel:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content):
        self.sent.append(content)
        return FakeMessage(content)

    def typing(self):
        return _AsyncNull()


class _AsyncNull:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def bot(tmp_path: Path) -> Quartermaster:
    return Quartermaster(Settings(vault=tmp_path, discord_owner_id=1))


async def test_stop_cancels_the_running_turn(tmp_path, monkeypatch):
    started = asyncio.Event()
    seen: list[agent.Profile] = []

    async def slow_ask(prompt, profile, cli, on_progress=None):
        seen.append(profile)
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(agent, "ask", slow_ask)
    qm, ch = bot(tmp_path), FakeChannel()
    turn = asyncio.create_task(qm.run_turn(ch, "hi", qm.owner))
    await started.wait()
    assert await qm.command(ch, "!stop")
    await asyncio.wait_for(turn, 2)
    assert ch.sent[-1] == "⏹️ Stopped."
    assert not qm._busy.locked() and qm._turn is None


async def test_stop_with_nothing_running_says_so(tmp_path):
    qm, ch = bot(tmp_path), FakeChannel()
    assert await qm.command(ch, "!STOP ")
    assert ch.sent == ["Nothing is running."]


async def test_new_makes_exactly_one_turn_skip_the_shared_session(tmp_path):
    qm, ch = bot(tmp_path), FakeChannel()
    assert await qm.command(ch, "!new")
    assert qm._fresh
    assert not await qm.command(ch, "what's on today")


async def test_plain_words_control_the_session(tmp_path):
    qm, ch = bot(tmp_path), FakeChannel()
    for said in ("stop", "Cancel.", "nvm", "never mind", "forget it"):
        assert await qm.command(ch, said), said
    assert await qm.command(ch, "start fresh")
    assert qm._fresh
    # Only a whole, short message counts - a real request still goes to the model.
    for request in ("stop reminding me about the Blues", "what's new", "cancel my 3pm"):
        assert not await qm.command(ch, request), request
