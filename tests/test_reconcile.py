"""Reconcile: bloat removed in code-checked edits, disagreements asked, never
guessed. And `qm quit`, which picks what to kill."""

from datetime import date
from pathlib import Path

import pytest

from quartermaster import agent, procs, reconcile
from quartermaster.config import DEFAULTS, Settings
from quartermaster.schedule import RECONCILE_TASK, build_tasks


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    v = tmp_path / "Vault"
    (v / "facts").mkdir(parents=True)
    (v / "notion" / "lifestyle").mkdir(parents=True)
    (v / "90-System").mkdir()
    (v / "facts" / "plans.md").write_text(
        "# Plans\n\n- 9/29: the concert. Still has to buy tickets.\n- 10/3: dinner.\n"
        "- 10/3: dinner at the usual place.\n- 8/1: something long past.\n", "utf-8")
    (v / "facts" / "interests.md").write_text("# Interests\n\n- Music: already going to the 9/29 concert.\n", "utf-8")
    (v / "facts" / "README.md").write_text("# Facts\n", "utf-8")
    (v / "notion" / "lifestyle" / "wishlist-12345678.md").write_text('---\ntitle: "Wishlist"\n---\nA standing desk and monitors.\n', "utf-8")
    (v / "notion" / "empty-87654321.md").write_text('---\ntitle: "Empty"\n---\n\n', "utf-8")
    return Settings(vault=v, prefs=DEFAULTS)


def test_gather_skips_readmes_and_empty_pages(settings):
    facts, notion = reconcile.gather(settings)
    assert set(facts) == {"facts/plans.md", "facts/interests.md"}
    assert "notion/lifestyle/wishlist-12345678.md" in notion and "Empty" not in notion and "title:" not in notion
    prompt = reconcile.build_prompt(facts, notion, date(2026, 9, 22))
    assert "Today is 2026-09-22" in prompt and "=== facts/plans.md ===" in prompt


def test_apply_edits_keeps_a_backup_and_respects_the_guards(settings):
    facts, _ = reconcile.gather(settings)
    plans = settings.vault / "facts" / "plans.md"
    interests = settings.vault / "facts" / "interests.md"
    edits = [
        {"file": "facts/plans.md", "summary": "merged the two dinner lines, dropped 8/1",
         "content": "# Plans\n\n- 9/29: the concert. Still has to buy tickets.\n- 10/3: dinner at the usual place.\n"},
        {"file": "facts/interests.md", "summary": "gutted", "content": "#"},  # more than 60% cut
        {"file": "notion/lifestyle/wishlist-12345678.md", "summary": "x", "content": "hijacked"},
        {"file": "facts/../.mcp.json", "summary": "x", "content": "{}"},
        {"file": "facts/new.md", "summary": "x", "content": "invented"},
    ]
    applied, skipped = reconcile.apply_edits(settings, edits, facts)
    assert applied == ["facts/plans.md: merged the two dinner lines, dropped 8/1"]
    assert "8/1" not in plans.read_text("utf-8")
    assert "Music" in interests.read_text("utf-8")
    assert len(skipped) == 4
    assert not (settings.vault / "facts" / "new.md").exists()
    assert "hijacked" not in (settings.vault / "notion" / "lifestyle" / "wishlist-12345678.md").read_text("utf-8")
    backups = list((settings.system_dir / "backups" / "reconcile").rglob("plans.md"))
    assert len(backups) == 1 and "8/1" in backups[0].read_text("utf-8")


def test_a_file_changed_mid_run_is_left_alone(settings):
    facts, _ = reconcile.gather(settings)
    interests = settings.vault / "facts" / "interests.md"
    interests.write_text(interests.read_text("utf-8") + "- Retro gaming.\n", "utf-8")  # the agent, meanwhile
    applied, skipped = reconcile.apply_edits(
        settings, [{"file": "facts/interests.md", "summary": "s", "content": "# Interests\n\n- Music.\n"}], facts)
    assert applied == [] and "changed while reconciling" in skipped[0]
    assert "Retro gaming" in interests.read_text("utf-8")


CONFLICT = {"about": "Concert tickets",
            "claims": ["facts/plans.md: still has to buy tickets", "facts/interests.md: already going"],
            "question": "Do you already have tickets for the 9/29 concert?"}


def test_conflicts_are_written_shown_to_the_owner_and_cleared(settings):
    path = reconcile.conflicts_path(settings)
    assert reconcile.for_prompt(path) == ""
    reconcile.write_conflicts(settings, [CONFLICT])
    assert "## Concert tickets" in path.read_text("utf-8")
    block = agent._options(agent.owner_profile(settings)).system_prompt["append"]
    assert "Do you already have tickets" in block and "delete that entry" in block
    assert "Concert" not in agent._options(agent.digest_profile(settings)).system_prompt["append"]
    reconcile.write_conflicts(settings, [])  # next run: settled
    assert reconcile.for_prompt(path) == "" and "Nothing open" in path.read_text("utf-8")


def test_run_applies_asks_and_dms(settings, monkeypatch):
    sent = []

    async def fake_ask(prompt, profile, cli):
        assert profile.name == "reconcile" and profile.tools == [] and profile.output_schema
        return agent.Reply(text="", structured={"edits": [], "conflicts": [CONFLICT]})

    monkeypatch.setattr(agent, "ask", fake_ask)
    monkeypatch.setattr("quartermaster.surfaces.digest_send.send_dm", lambda s, text: sent.append(text))
    assert "Do you already have tickets" in reconcile.run_reconcile(settings, dry_run=True)
    assert sent == [] and not reconcile.conflicts_path(settings).exists()
    reconcile.run_reconcile(settings)
    assert len(sent) == 1 and "which is right" in sent[0]


def test_quiet_when_consistent(settings, monkeypatch):
    async def fake_ask(prompt, profile, cli):
        return agent.Reply(text="", structured={"edits": [], "conflicts": []})

    monkeypatch.setattr(agent, "ask", fake_ask)
    monkeypatch.setattr("quartermaster.surfaces.digest_send.send_dm", lambda s, text: pytest.fail("sent a DM"))
    assert "nothing sent" in reconcile.run_reconcile(settings)


def test_scheduled_daily_after_the_sync(settings):
    task = next(t for t in build_tasks(settings, "daily") if t.name == RECONCILE_TASK)
    assert task.command[-1] == "reconcile" and "07:30" in task.schedule_args


def test_structured_output_is_allowed_only_with_a_schema(settings):
    # The SDK answers an output_schema through a StructuredOutput tool call;
    # denying it made the reconcile job loop until max_turns.
    assert agent.check_tool(agent.reconcile_profile(settings, reconcile.SCHEMA), "StructuredOutput", {}) is None
    assert agent.check_tool(agent.parser_profile(settings, {"type": "object"}), "StructuredOutput", {}) is None
    assert agent.check_tool(agent.owner_profile(settings), "StructuredOutput", {})
    assert agent.check_tool(agent.digest_profile(settings), "StructuredOutput", {})


def test_owner_is_told_to_propagate_changes():
    assert "every place that states it" in agent.OWNER_LIMITS


# --- qm quit -------------------------------------------------------------------

def proc(pid, ppid, name, cmd=""):
    return {"pid": pid, "ppid": ppid, "name": name, "cmd": cmd}


def test_quit_targets_every_qm_tree_but_its_own():
    table = [
        proc(1, 0, "explorer.exe"),
        proc(10, 1, "powershell.exe"),
        proc(11, 10, "qm.exe", "qm.exe quit"),               # this command's launcher
        proc(12, 11, "python.exe", "python quartermaster quit"),  # this command
        proc(20, 1, "qm.exe", "qm.exe bot"),                 # the bot
        proc(21, 20, "python.exe", "python -m quartermaster.cli bot"),  # its child: covered by the tree
        proc(22, 21, "claude.exe", "claude.exe --print"),
        proc(30, 1, "qm.exe", "qm.exe web --port 8766"),
        proc(40, 1, "python.exe", "python -m quartermaster.cli mcp qm"),  # an orphaned MCP server
        proc(50, 1, "python.exe", "python some_other_script.py"),
        proc(60, 1, "claude.exe", "claude"),                 # the owner's own terminal session
        # VS Code's isort server runs on the venv's python, whose path says Quartermaster.
        proc(70, 1, "python.exe", r"C:\Coding\Quartermaster\.venv\Scripts\python.exe c:\ext\isort\lsp_server.py"),
        proc(80, 1, "python.exe", r'"C:\Coding\Quartermaster\.venv\Scripts\python.exe" "C:\Coding\Quartermaster\.venv\Scripts\qm.exe" digest'),
    ]
    assert [p["pid"] for p in procs.targets(table, self_pid=12)] == [20, 30, 40, 80]
    assert procs.describe(table[4]) == "bot (pid 20)" and procs.describe(table[7]) == "web (pid 30)"
