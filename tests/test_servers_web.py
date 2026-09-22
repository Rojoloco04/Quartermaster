"""/servers: a tab per game, its status, start/stop behind the CSRF token, and
the console tailed (colour codes stripped, cleared when a new run truncates it)."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from quartermaster.config import DEFAULTS, Settings
from quartermaster.integrations import game_servers, minecraft
from quartermaster.surfaces import web


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setattr(Settings, "log_path", property(lambda self: tmp_path / "logs" / "quartermaster.log"))
    (tmp_path / "Vault" / "90-System").mkdir(parents=True)
    (tmp_path / "mc").mkdir()
    return Settings(vault=tmp_path / "Vault",
                    prefs={**DEFAULTS, "minecraft": {"dir": str(tmp_path / "mc"), "memory_gb": 4}})


@pytest.fixture
def client(settings, monkeypatch) -> TestClient:
    monkeypatch.setattr(minecraft, "status", lambda s: "Running (Paper 26.2). There are 0 of a max of 20 players online:")
    return TestClient(web.build_app(settings), base_url="http://127.0.0.1")


def csrf_of(html: str) -> str:
    return html.split('name="qm-csrf" content="', 1)[1].split('"', 1)[0]


def test_every_game_gets_a_tab_and_the_nav_links_it(client):
    html = client.get("/servers").text
    assert "Running (Paper 26.2)" in html
    for game in game_servers.GAMES.values():
        assert f"href='/servers/{game.key}'" in html
    assert 'href="/servers"' in client.get("/").text


def test_unknown_game_is_404(client):
    assert client.get("/servers/tetris").status_code == 404
    assert client.get("/api/servers/tetris/log").status_code == 404


def test_console_is_tailed_without_colour_codes(client, settings):
    minecraft.log_path(settings).write_bytes(b"\x1b[32m[12:00:01 INFO]: Done (3.2s)!\x1b[0m\n")
    d = client.get("/api/servers/minecraft/log?pos=0").json()
    assert d["text"] == "[12:00:01 INFO]: Done (3.2s)!\n" and not d["reset"]
    minecraft.log_path(settings).write_bytes(b"new run\n")  # truncated by a restart
    d2 = client.get(f"/api/servers/minecraft/log?pos={d['pos']}").json()
    assert d2["reset"] and d2["text"] == "new run\n"


def test_rcon_chatter_is_hidden_and_partial_lines_wait(client, settings):
    minecraft.log_path(settings).write_bytes(
        b"[17:52:02 INFO]: Thread RCON Client /127.0.0.1 started\n"
        b"[17:52:03 INFO]: Steve joined the game\n"
        b"[17:52:02 INFO]: Thread RCON Client /127.0.0.1 shutting down\n"
        b"[17:52:04 INFO]: half a li"
    )
    d = client.get("/api/servers/minecraft/log?pos=0").json()
    assert d["text"] == "[17:52:03 INFO]: Steve joined the game\n"
    with minecraft.log_path(settings).open("ab") as fh:
        fh.write(b"ne\n")
    assert client.get(f"/api/servers/minecraft/log?pos={d['pos']}").json()["text"] == "[17:52:04 INFO]: half a line\n"


def test_start_and_stop_need_the_page_token(client, monkeypatch):
    calls = []
    monkeypatch.setattr(minecraft, "start", lambda s: calls.append("start") or "Starting")
    csrf = csrf_of(client.get("/servers/minecraft").text)
    assert client.post("/api/servers/minecraft/start").status_code == 403
    r = client.post("/api/servers/minecraft/start", headers={"X-QM-CSRF": csrf})
    assert r.status_code == 200 and r.json()["result"] == "Starting" and calls == ["start"]


def test_only_start_and_stop_are_actions(client):
    csrf = csrf_of(client.get("/servers").text)
    assert client.post("/api/servers/minecraft/setup", headers={"X-QM-CSRF": csrf}).status_code == 404


def test_a_refusal_is_shown_not_crashed(client, monkeypatch):
    def refuse(s):
        raise minecraft.MinecraftError("Not set up yet.")

    monkeypatch.setattr(minecraft, "stop", refuse)
    csrf = csrf_of(client.get("/servers").text)
    r = client.post("/api/servers/minecraft/stop", headers={"X-QM-CSRF": csrf})
    assert r.status_code == 409 and r.json()["error"] == "Not set up yet."
