"""The `qm web` dashboard: its parsers, and the token gate."""

import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from quartermaster import agent
from quartermaster.config import DEFAULTS, Settings
from quartermaster.surfaces import web

LOG = """\
2026-09-22 08:32:49,705 INFO quartermaster.agent: [aaaa1111] owner turn start (model=claude-sonnet-5): what's on today
2026-09-22 08:32:58,942 INFO quartermaster.agent: [aaaa1111] tool call: Glob({'pattern': 'x'})
2026-09-22 08:33:17,474 INFO quartermaster.agent: [aaaa1111] tool call: mcp__google__list_events({})
2026-09-22 08:36:19,838 INFO quartermaster.agent: [aaaa1111] turn done: ok=True cost=$0.0412 session=s1
2026-09-22 09:00:00,000 INFO quartermaster.agent: [bbbb2222] owner turn start (model=claude-haiku-4-5): count to 400
2026-09-22 09:00:06,000 INFO quartermaster.agent: [bbbb2222] cancelled (profile=owner)
2026-09-22 09:01:00,000 INFO quartermaster.agent: [cccc3333] owner turn start (model=claude-opus-5): still going
"""


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setattr(Settings, "log_path", property(lambda self: tmp_path / "logs" / "quartermaster.log"))
    return Settings(vault=tmp_path / "Vault", prefs=DEFAULTS)


def test_recent_turns_newest_first_with_outcome():
    turns = web.recent_turns(LOG)
    assert [t["id"] for t in turns] == ["cccc3333", "bbbb2222", "aaaa1111"]
    assert turns[0]["ok"] is None  # still running
    assert turns[1]["ok"] is False  # cancelled
    assert turns[2] == {**turns[2], "ok": True, "cost": "0.0412", "tools": 2, "profile": "owner"}


def test_bot_status_reads_the_heartbeat(settings):
    assert web.bot_status(settings)[0] is False
    path = web.heartbeat_path(settings)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 42, "at": time.time()}))
    assert web.bot_status(settings) == (True, web.bot_status(settings)[1])
    assert web.bot_status(settings, now=time.time() + 600)[0] is False


def test_session_entries_keep_text_and_label_the_surface(tmp_path):
    lines = [
        {"type": "user", "entrypoint": "sdk-py", "timestamp": "2026-09-22T08:00:00Z", "message": {"content": "from discord"}},
        {"type": "assistant", "entrypoint": "sdk-py", "message": {"content": [{"type": "tool_use"}]}},
        {"type": "assistant", "entrypoint": "sdk-py", "message": {"content": [{"type": "text", "text": "reply"}]}},
        {"type": "user", "entrypoint": "claude-vscode", "message": {"content": "from terminal"}},
        {"type": "attachment"},
    ]
    (tmp_path / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    sid, entries = web.session_entries(tmp_path)
    assert sid == "s1"
    assert [(e["text"], e["source"]) for e in entries] == [
        ("from discord", "discord"), ("reply", "discord"), ("from terminal", "terminal")]


def test_read_log_from_tails_and_follows(tmp_path):
    log = tmp_path / "q.log"
    log.write_text("abcdef")
    pos, text = web.read_log_from(log, -3)
    assert text == "def"
    log.write_text("abcdefgh")
    assert web.read_log_from(log, pos) == (8, "gh")
    assert web.read_log_from(log, 99)[1] == "abcdefgh"  # rotated: start over


def test_markdown_escapes_html():
    out = web.markdown_to_html("# Title\n- `!stop` <b>\n```\n<script>\n```")
    assert "<h2>Title</h2>" in out and "<code>!stop</code>" in out
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_token_gate(settings, monkeypatch):
    monkeypatch.setattr(web, "dashboard", lambda s: "<p>dash</p>")
    client = TestClient(web.build_app(settings, token="s3cret"))
    assert client.get("/").status_code == 401
    assert client.get("/?token=wrong").status_code == 401
    assert client.get("/?token=s3cret").status_code == 200  # redirected, cookie set
    assert client.get("/").text == "<p>dash</p>"
    assert client.get("/digest/..%2F..%2Fetc").status_code == 404


def test_refuses_to_serve_beyond_localhost_without_a_token(settings, monkeypatch):
    monkeypatch.delenv("QM_WEB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="QM_WEB_TOKEN"):
        web.serve(settings, host="100.64.0.1")


def test_digest_profile_never_runs_in_the_vault(settings):
    # A digest session in the vault became the newest one there, and the owner's
    # next DM (continue_conversation) resumed it instead of their own thread.
    profile = agent.digest_profile(settings)
    assert profile.cwd != settings.vault and settings.vault not in profile.cwd.parents
