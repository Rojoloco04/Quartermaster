"""Notion edits outside the Claude page: proposed by the agent, applied only
after the owner presses Confirm."""

from pathlib import Path

import pytest

from quartermaster import claude_tidy, db, notion_writes
from quartermaster.config import DEFAULTS, Settings
from quartermaster.integrations.notion import NotionError


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    prefs = {**DEFAULTS, "notion": {**DEFAULTS["notion"], "claude_page_id": "c" * 32}}
    return Settings(vault=tmp_path / "Vault", notion_token="t", prefs=prefs)


@pytest.fixture
def conn(settings: Settings):
    with db.session(settings.db_path) as c:
        yield c


class FakeClient:
    """Stands in for NotionClient; records what it was asked to do."""

    instance = None

    def __init__(self, token, timeout=30.0):
        self.appended: list[tuple[str, str]] = []
        self.replaced: list[tuple[str, str]] = []
        self.trashed: list[str] = []
        FakeClient.instance = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def page_markdown(self, page_id):
        from quartermaster.integrations.notion import PageMarkdown

        return PageMarkdown(markdown="the old contents", truncated=False, unknown_block_ids=[])

    def append_markdown(self, page_id, markdown):
        self.appended.append((page_id, markdown))

    def replace_markdown(self, page_id, markdown):
        self.replaced.append((page_id, markdown))

    def trash_page(self, page_id):
        self.trashed.append(page_id)


def test_proposing_writes_nothing_until_applied(settings, conn, monkeypatch):
    monkeypatch.setattr(notion_writes, "NotionClient", FakeClient)
    FakeClient.instance = None
    write_id = notion_writes.propose(conn, "AB-CD", "Groceries", "append", "- milk", why="asked for")
    row = notion_writes.pending(conn)[0]
    assert row["id"] == write_id and row["status"] == "pending"
    assert row["page_id"] == "abcd"  # dashes and case normalised
    assert FakeClient.instance is None

    assert "✅" in notion_writes.apply(settings, conn, row)
    assert FakeClient.instance.appended == [("abcd", "- milk")]
    assert notion_writes.pending(conn) == []


def test_replace_backs_the_old_page_up_into_the_vault(settings, conn, monkeypatch):
    monkeypatch.setattr(notion_writes, "NotionClient", FakeClient)
    notion_writes.propose(conn, "ab", "Reading list", "replace", "tidied", source="tidy")
    row = notion_writes.pending(conn)[0]
    result = notion_writes.apply(settings, conn, row)
    backup = notion_writes.backup_path(settings, row)
    assert backup.exists() and "the old contents" in backup.read_text(encoding="utf-8")
    assert FakeClient.instance.replaced == [("ab", "tidied")]
    assert backup.name in result


def test_declining_leaves_the_page_alone(settings, conn, monkeypatch):
    monkeypatch.setattr(notion_writes, "NotionClient", FakeClient)
    FakeClient.instance = None
    notion_writes.propose(conn, "ab", "Notes", "append", "x")
    notion_writes.decide(conn, notion_writes.pending(conn)[0]["id"], "declined")
    assert notion_writes.pending(conn) == []
    assert FakeClient.instance is None


def test_a_failed_apply_is_recorded_not_retried_forever(settings, conn, monkeypatch):
    class Boom(FakeClient):
        def append_markdown(self, page_id, markdown):
            raise NotionError("Notion said no")

    monkeypatch.setattr(notion_writes, "NotionClient", Boom)
    notion_writes.propose(conn, "ab", "Notes", "append", "x")
    assert "❌" in notion_writes.apply(settings, conn, notion_writes.pending(conn)[0])
    assert notion_writes.pending(conn) == []


def test_preview_is_built_from_the_row(settings, conn):
    notion_writes.propose(conn, "ab", "Reading list", "replace", "a" * 2000, why="weekly tidy")
    text = notion_writes.preview(notion_writes.pending(conn)[0])
    assert "Replace" in text and "Reading list" in text and "weekly tidy" in text
    assert "more characters)" in text and "saved to the vault" in text


def test_delete_backs_up_then_trashes(settings, conn, monkeypatch):
    monkeypatch.setattr(notion_writes, "NotionClient", FakeClient)
    notion_writes.propose(conn, "AB", "Old plan", "delete", "", why="owner asked")
    row = notion_writes.pending(conn)[0]
    text = notion_writes.preview(row)
    assert "Delete" in text and "Old plan" in text and "trash" in text and "```" not in text
    result = notion_writes.apply(settings, conn, row)
    assert FakeClient.instance.trashed == ["ab"] and FakeClient.instance.replaced == []
    backup = notion_writes.backup_path(settings, row)
    assert "the old contents" in backup.read_text(encoding="utf-8") and backup.name in result


def test_delete_tool_refuses_the_claude_page_and_unknown_pages(settings, monkeypatch):
    from quartermaster import servers
    from quartermaster.servers import qm
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setattr(servers, "settings", lambda: settings)
    monkeypatch.setattr(qm, "settings", lambda: settings)
    with db.session(settings.db_path) as conn:
        conn.execute("INSERT INTO notion_pages (page_id, vault_path, title, synced_at) VALUES (?, ?, ?, ?)",
                     ("d" * 32, "notion/x.md", "Quartermaster", db.utcnow()))
        conn.commit()
    with pytest.raises(ToolError, match="Claude page itself"):
        qm.propose_notion_delete("c" * 32)
    with pytest.raises(ToolError, match="No mirrored page"):
        qm.propose_notion_delete("e" * 32)
    with pytest.raises(ToolError, match="propose_notion_delete"):
        qm.propose_notion_edit("d" * 32, "x", mode="delete")
    assert "Nothing is deleted yet" in qm.propose_notion_delete("d" * 32, why="asked")
    with db.session(settings.db_path) as conn:
        row = notion_writes.pending(conn)[0]
        assert row["mode"] == "delete" and row["page_title"] == "Quartermaster"


def test_bad_proposals_are_refused(conn):
    with pytest.raises(NotionError):
        notion_writes.propose(conn, "ab", "Notes", "archive", "x")
    with pytest.raises(NotionError):
        notion_writes.propose(conn, "ab", "Notes", "append", "   ")


class TestTidy:
    def test_a_tidy_that_guts_the_page_is_not_proposed(self, settings, monkeypatch):
        monkeypatch.setattr(claude_tidy, "NotionClient", lambda *a, **k: FakeTidyClient("keep " * 200))
        monkeypatch.setattr(claude_tidy.agent, "ask", _answering('tiny'))
        assert "more than half" in claude_tidy.run_tidy(settings)
        with db.session(settings.db_path) as conn:
            assert notion_writes.pending(conn) == []

    def test_no_changes_proposes_nothing(self, settings, monkeypatch):
        monkeypatch.setattr(claude_tidy, "NotionClient", lambda *a, **k: FakeTidyClient("keep " * 200))
        monkeypatch.setattr(claude_tidy.agent, "ask", _answering(claude_tidy.NO_CHANGES))
        assert "nothing proposed" in claude_tidy.run_tidy(settings)

    def test_a_real_tidy_is_proposed_for_confirmation(self, settings, monkeypatch):
        monkeypatch.setattr(claude_tidy, "NotionClient", lambda *a, **k: FakeTidyClient("keep " * 200))
        monkeypatch.setattr(claude_tidy.agent, "ask", _answering("keep " * 120))
        assert "Proposed tidy" in claude_tidy.run_tidy(settings)
        with db.session(settings.db_path) as conn:
            row = notion_writes.pending(conn)[0]
            assert row["mode"] == "replace" and row["source"] == "tidy"


class FakeTidyClient(FakeClient):
    def __init__(self, text):
        self.text = text

    def page_markdown(self, page_id):
        from quartermaster.integrations.notion import PageMarkdown

        return PageMarkdown(markdown=self.text, truncated=False, unknown_block_ids=[])


def _answering(text):
    """A stand-in for agent.ask: awaited, so it must be a coroutine."""

    async def ask(*args, **kwargs):
        return Reply(text)

    return ask


class Reply:
    """agent.ask's result, without running a model."""

    def __init__(self, text):
        self.text, self.error = text, None

    @property
    def ok(self):
        return True
