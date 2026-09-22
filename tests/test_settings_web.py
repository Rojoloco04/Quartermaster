"""/settings: everything configurable is visible and editable, and nothing
else is writable."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from quartermaster.config import DEFAULTS, Settings
from quartermaster.surfaces import web


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "Vault"
    (v / "90-System").mkdir(parents=True)
    (v / "facts").mkdir()
    (v / "notion").mkdir()
    (v / "90-System" / "config.toml").write_text('[digest]\nhour = 7\n', "utf-8")
    (v / "90-System" / "muted.md").write_text("# Muted\n\nartist/Tool\n", "utf-8")
    (v / "facts" / "about.md").write_text("# About the owner\n\n- Drives a hatchback.\n", "utf-8")
    (v / "notion" / "page-12345678.md").write_text("# Mirror\n", "utf-8")
    (v / "CLAUDE.md").write_text("# Quartermaster\n", "utf-8")
    return v


@pytest.fixture
def client(vault, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(Settings, "log_path", property(lambda self: tmp_path / "logs" / "quartermaster.log"))
    return TestClient(web.build_app(Settings(vault=vault, prefs=DEFAULTS)), base_url="http://127.0.0.1")


def csrf_of(html: str) -> str:
    return html.split('name="qm-csrf" content="', 1)[1].split('"', 1)[0]


def test_only_config_and_facts_are_editable(vault):
    for rel in ("CLAUDE.md", "90-System/config.toml", "90-System/muted.md", "90-System/dev-queue.md",
                "facts/about.md", "facts/lessons.md"):
        assert web.editable_path(vault, rel), rel
    for rel in ("notion/page-12345678.md", "digests/x.md", "inbox/x.md", ".mcp.json", ".claude/settings.json",
                "facts/../.mcp.json", "facts/sub/x.md", "90-System/state.db", "../outside.md"):
        assert web.editable_path(vault, rel) is None, rel


def test_save_refuses_stale_edits_and_bad_toml(vault):
    path = vault / "facts" / "about.md"
    loaded = web.file_hash(path)
    path.write_text(path.read_text("utf-8") + "- Agent wrote this meanwhile.\n", "utf-8")
    with pytest.raises(web.EditRefused, match="changed since"):
        web.save_file(vault, "facts/about.md", "# About\n", loaded)

    toml = vault / "90-System" / "config.toml"
    with pytest.raises(web.EditRefused, match="doesn't parse"):
        web.save_file(vault, "90-System/config.toml", "[digest\nhour = ", web.file_hash(toml))
    assert "hour = 7" in toml.read_text("utf-8")

    new = web.save_file(vault, "90-System/config.toml", "[digest]\r\nhour = 9", web.file_hash(toml))
    assert toml.read_text("utf-8") == "[digest]\nhour = 9\n" and new == web.file_hash(toml)
    # A new fact file: nothing loaded, so the expected hash is empty.
    web.save_file(vault, "facts/cars.md", "# Cars\n", "")
    assert (vault / "facts" / "cars.md").exists()


def test_effective_prefs_mark_what_you_set(vault):
    rows = {key: (value, yours) for key, value, yours in web.effective_prefs(vault)}
    assert rows["digest.hour"] == ("7", True)
    assert rows["digest.weekday"] == ('"sunday"', False)


def test_set_pref_edits_one_value_and_keeps_comments(vault):
    toml = vault / "90-System" / "config.toml"
    toml.write_text("# mine\n[digest]\nhour = 7  # evenings\n\n[home]\nlabel = \"STL\"\n", "utf-8")
    web.set_pref(vault, "digest.hour", "9", web.file_hash(toml))
    # A default not in the file yet: a new section is added at the end.
    web.set_pref(vault, "chat.fresh_after_minutes", "30", web.file_hash(toml))
    # A missing key in an existing section goes into that section; quotes optional for strings.
    web.set_pref(vault, "digest.weekday", "saturday", web.file_hash(toml))
    text = toml.read_text("utf-8")
    assert text.startswith("# mine\n[digest]\nhour = 9\nweekday = \"saturday\"\n")
    assert text.endswith('[chat]\nfresh_after_minutes = 30\n')
    rows = {key: (value, yours) for key, value, yours in web.effective_prefs(vault)}
    assert rows["chat.fresh_after_minutes"] == ("30", True) and rows["home.label"] == ('"STL"', True)


def test_set_pref_refuses_wrong_types_lists_and_stale_hashes(vault):
    toml = vault / "90-System" / "config.toml"
    h = web.file_hash(toml)
    with pytest.raises(web.EditRefused, match="whole number"):
        web.set_pref(vault, "digest.hour", "evening", h)
    with pytest.raises(web.EditRefused, match="list"):
        web.set_pref(vault, "events.bands", "[]", h)
    with pytest.raises(web.EditRefused, match="can't be set"):
        web.set_pref(vault, "digest.nope", "1", h)
    with pytest.raises(web.EditRefused, match="changed since"):
        web.set_pref(vault, "digest.hour", "8", "stale")
    assert toml.read_text("utf-8") == "[digest]\nhour = 7\n"


def test_pref_endpoint_needs_the_page_token(client, vault):
    html = client.get("/settings").text
    assert "data-pref" in html
    toml = vault / "90-System" / "config.toml"
    body = {"key": "chat.fresh_after_minutes", "value": "15", "hash": web.file_hash(toml)}
    assert client.post("/api/pref", json=body).status_code == 403
    assert client.post("/api/pref", json=body, headers={"X-QM-CSRF": csrf_of(html)}).status_code == 200
    assert "fresh_after_minutes = 15" in toml.read_text("utf-8")


def test_chat_pref_is_read_fresh_each_turn(vault):
    from quartermaster.config import current_prefs
    s = Settings(vault=vault, prefs=DEFAULTS)
    assert current_prefs(s)["chat"]["fresh_after_minutes"] == 5
    (vault / "90-System" / "config.toml").write_text("[chat]\nfresh_after_minutes = 0\n", "utf-8")
    assert current_prefs(s)["chat"]["fresh_after_minutes"] == 0


def test_secrets_show_set_or_not_never_values(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_supersecret")
    monkeypatch.delenv("KLIPY_API_KEY", raising=False)
    status = dict(web.secret_status())
    assert status["NOTION_TOKEN"] is True and status["KLIPY_API_KEY"] is False


def test_settings_page_shows_everything_but_secret_values(client, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_supersecret")
    html = client.get("/settings").text
    for expected in ("digest.hour", "About the owner", "facts/lessons.md", "artist/Tool", "90-System/dev-queue.md", "NOTION_TOKEN"):
        assert expected in html, expected
    assert "ntn_supersecret" not in html


def test_save_endpoint_needs_the_page_token(client, vault):
    csrf = csrf_of(client.get("/settings").text)
    path = vault / "90-System" / "muted.md"
    body = {"path": "90-System/muted.md", "text": "# Muted\n\nartist/Tool\nprice:abc\n", "hash": web.file_hash(path)}
    assert client.post("/api/file", json=body).status_code == 403
    assert client.post("/api/file", json=body, headers={"X-QM-CSRF": "guess"}).status_code == 403
    r = client.post("/api/file", json=body, headers={"X-QM-CSRF": csrf})
    assert r.status_code == 200 and "price:abc" in path.read_text("utf-8") and "price:abc" in r.json()["html"]
    # Same stale hash again: refused, not clobbered.
    assert client.post("/api/file", json=body, headers={"X-QM-CSRF": csrf}).status_code == 409
    bad = {**body, "path": "notion/page-12345678.md", "hash": ""}
    assert client.post("/api/file", json=bad, headers={"X-QM-CSRF": csrf}).status_code == 409


def test_unexpected_host_is_refused(vault, tmp_path, monkeypatch):
    # A DNS-rebinding page reaches 127.0.0.1 under its own hostname.
    monkeypatch.setattr(Settings, "log_path", property(lambda self: tmp_path / "logs" / "quartermaster.log"))
    rebound = TestClient(web.build_app(Settings(vault=vault, prefs=DEFAULTS)), base_url="http://evil.example")
    assert rebound.get("/settings").status_code == 400


def test_architecture_page_is_served_with_the_shared_nav(client):
    html = client.get("/architecture").text
    assert "How Quartermaster fits together" in html and "check_tool" in html
    assert web.NAV in html and html.count("<nav>") == 1


def test_brain_panel_can_edit_facts_not_the_mirror(client):
    fact = client.get("/api/brain/note", params={"id": "facts/about.md"}).json()
    assert fact["editable"] and "hatchback" in fact["raw"] and fact["hash"]
    mirror = client.get("/api/brain/note", params={"id": "notion/page-12345678.md"}).json()
    assert mirror["editable"] is False and "raw" not in mirror
