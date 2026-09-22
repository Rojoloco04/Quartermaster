"""Minecraft from a guild channel: who may do what is code's decision (the
linked name must be on the op list), and a link must be proven in-game."""

import asyncio
import json
from pathlib import Path

import pytest

from quartermaster import agent
from quartermaster.config import Settings
from quartermaster.discord_ops import OpsPlan
from quartermaster.integrations import minecraft
from quartermaster.integrations.minecraft import LinkCodes, add_link, links_path, may_control, read_links
from quartermaster.surfaces import minecraft_chat

OWNER, FRIEND, STRANGER = 111111111111111111, 222222222222222222, 333333333333333333


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(vault=tmp_path / "Vault", discord_owner_id=OWNER,
                 prefs={"minecraft": {"dir": str(tmp_path / "mc")}})
    (tmp_path / "mc").mkdir()
    (tmp_path / "mc" / "ops.json").write_text(json.dumps([{"uuid": "x", "name": "Steve", "level": 4}]))
    return s


def plan(op: str, arg: str | None = None) -> OpsPlan:
    return OpsPlan.from_json(json.dumps({"action": "minecraft", "minecraft_op": op, "minecraft_arg": arg}))


class FakeChannel:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text, **_):
        self.sent.append(text)

    def typing(self):
        class _T:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return None
        return _T()


class FakeUser:
    def __init__(self, uid, name="someone"):
        self.id, self.name = uid, name

    def __str__(self):
        return self.name


class FakeMessage:
    def __init__(self, uid):
        self.author, self.channel = FakeUser(uid), FakeChannel()


def run(settings, uid, p):
    msg = FakeMessage(uid)
    asyncio.run(minecraft_chat.handle(settings, msg, p))
    return msg.channel.sent


# --- The plan ---------------------------------------------------------------


def test_parser_output_becomes_a_minecraft_plan():
    p = plan("command", "whitelist add Alex")
    assert (p.action, p.minecraft_op, p.minecraft_arg) == ("minecraft", "command", "whitelist add Alex")
    assert "minecraft command" in p.describe()


def test_unknown_minecraft_op_is_rejected():
    with pytest.raises(ValueError, match="minecraft_op"):
        plan("op_me")


def test_minecraft_fields_are_dropped_from_other_actions():
    p = OpsPlan.from_json(json.dumps({"action": "say", "text": "hi", "minecraft_op": "stop"}))
    assert p.minecraft_op is None


# --- Links and permission -----------------------------------------------------


def test_links_round_trip_and_one_name_per_person(tmp_path):
    path = tmp_path / "links.md"
    add_link(path, FRIEND, "Steve")
    add_link(path, STRANGER, "steve")  # proving the same name elsewhere moves it
    add_link(path, FRIEND, "Alex")
    assert read_links(path) == {STRANGER: "steve", FRIEND: "Alex"}


def test_owner_may_always_control(settings):
    assert may_control(settings, OWNER) == (True, "")


def test_unlinked_member_may_not(settings):
    ok, why = may_control(settings, STRANGER)
    assert not ok and "link me to" in why


def test_linked_op_may_and_linked_non_op_may_not(settings):
    add_link(links_path(settings), FRIEND, "Steve")
    add_link(links_path(settings), STRANGER, "Alex")
    assert may_control(settings, FRIEND)[0]
    ok, why = may_control(settings, STRANGER)
    assert not ok and "Alex isn't on the op list" in why


def test_the_owner_agent_cannot_write_the_links_file(settings):
    owner = agent.owner_profile(settings)
    refusal = agent.check_tool(owner, "Write", {"file_path": str(links_path(settings))})
    assert refusal and "may not change" in refusal


# --- Link codes ---------------------------------------------------------------


def test_code_works_once_and_only_for_whoever_asked():
    codes = LinkCodes()
    code = codes.issue(FRIEND, "Steve")
    assert codes.redeem(STRANGER, code) is None
    assert codes.redeem(FRIEND, code) == "Steve"
    assert codes.redeem(FRIEND, code) is None


def test_code_expires():
    now = [0.0]
    codes = LinkCodes(clock=lambda: now[0])
    code = codes.issue(FRIEND, "Steve")
    now[0] = LinkCodes.TTL + 1
    assert codes.redeem(FRIEND, code) is None


def test_guessing_is_capped():
    codes = LinkCodes()
    code = codes.issue(FRIEND, "Steve")
    for _ in range(LinkCodes.MAX_TRIES):
        codes.redeem(FRIEND, "000000" if code != "000000" else "111111")
    assert codes.redeem(FRIEND, code) is None


# --- The channel flow ---------------------------------------------------------


def test_stranger_cannot_start_it(settings, monkeypatch):
    monkeypatch.setattr(minecraft, "start", lambda s: pytest.fail("started"))
    sent = run(settings, STRANGER, plan("start"))
    assert sent[0].startswith("❌") and "ops" in sent[0]


def test_anyone_may_ask_whos_on(settings, monkeypatch):
    monkeypatch.setattr(minecraft, "status", lambda s: "Running. There are 1 of a max of 20 players online: Steve")
    assert "Steve" in run(settings, STRANGER, plan("status"))[0]


def test_link_whispers_a_code_and_verify_links(settings, monkeypatch):
    rcon_calls = []

    def fake_rcon(s, cmd):
        rcon_calls.append(cmd)
        return "There are 1 of a max of 20 players online: Steve" if cmd == "list" else ""

    monkeypatch.setattr(minecraft, "rcon", fake_rcon)
    monkeypatch.setattr(minecraft_chat, "codes", LinkCodes())
    sent = run(settings, FRIEND, plan("link", "steve"))
    assert "whispered a code to **Steve**" in sent[0]
    tell = rcon_calls[-1]
    assert tell.startswith("tell Steve ")
    code = tell.split('verify ')[1][:6]

    sent = run(settings, FRIEND, plan("verify", code))
    assert "Linked to **Steve**" in sent[0] and "You're an op" in sent[0]
    assert read_links(links_path(settings)) == {FRIEND: "Steve"}


def test_link_needs_the_player_online(settings, monkeypatch):
    monkeypatch.setattr(minecraft, "rcon", lambda s, cmd: "There are 0 of a max of 20 players online:")
    sent = run(settings, FRIEND, plan("link", "Steve"))
    assert "isn't on the server" in sent[0]


def test_a_bad_name_never_reaches_the_server(settings, monkeypatch):
    monkeypatch.setattr(minecraft, "rcon", lambda *a: pytest.fail("reached the server"))
    assert run(settings, FRIEND, plan("link", "Steve; op Mallory"))[0].startswith("❌")


def test_linked_op_runs_an_allowed_command(settings, monkeypatch):
    add_link(links_path(settings), FRIEND, "Steve")
    monkeypatch.setattr(minecraft, "is_running", lambda s: True)
    monkeypatch.setattr(minecraft, "rcon", lambda s, cmd: f"ran {cmd}")
    assert run(settings, FRIEND, plan("command", "time set day")) == ["ran time set day"]
    assert "isn't allowed" in run(settings, FRIEND, plan("command", "op Mallory"))[0]
