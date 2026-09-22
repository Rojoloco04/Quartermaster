"""The hourly vault push, against real git repos in a temp dir (a bare repo
stands in for the private remote)."""

import subprocess
from pathlib import Path

import pytest

from quartermaster.schedule import PUSH_TASK, build_tasks
from quartermaster.vault_push import run_push, summarize


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    remote, vault = tmp_path / "remote.git", tmp_path / "Vault"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "init", "-q", "-b", "main", str(vault))
    git(vault, "config", "user.name", "t")
    git(vault, "config", "user.email", "t@example.com")
    git(vault, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (vault / "CLAUDE.md").write_text("x\n")
    git(vault, "add", "-A")
    git(vault, "commit", "-q", "-m", "init")
    git(vault, "remote", "add", "origin", str(remote))
    git(vault, "push", "-q", "-u", "origin", "main")
    return vault


def remote_log(vault: Path) -> str:
    return git(vault.parent / "remote.git", "log", "--format=%s", "main")


def test_nothing_changed_pushes_nothing(vault):
    assert run_push(vault) == "Nothing to push."


def test_commits_new_changed_and_deleted_files_and_pushes_them(vault):
    (vault / "facts").mkdir()
    (vault / "facts" / "plans.md").write_text("tickets bought\n")
    (vault / "facts" / "interests.md").write_text("soccer\n")
    (vault / "CLAUDE.md").unlink()

    result = run_push(vault)

    assert "3 file(s)" in result and "pushed" in result
    assert remote_log(vault).splitlines()[0] == "qm push: facts 2, CLAUDE.md"
    assert git(vault, "status", "--porcelain") == ""


def test_ignored_files_stay_out(vault):
    (vault / ".gitignore").write_text("state.db\n")
    (vault / "state.db").write_text("machine state")
    run_push(vault)
    assert "state.db" not in git(vault, "ls-files")


def test_pushes_an_earlier_commit_that_never_made_it(vault):
    (vault / "a.md").write_text("a\n")
    git(vault, "add", "-A")
    git(vault, "commit", "-q", "-m", "by hand")
    assert run_push(vault) == "Pushed 1 earlier commit(s)."
    assert remote_log(vault).splitlines()[0] == "by hand"


def test_a_merge_in_progress_is_left_alone(vault):
    (vault / "new.md").write_text("n\n")
    (vault / ".git" / "MERGE_HEAD").write_text("0" * 40)
    with pytest.raises(RuntimeError, match="in progress"):
        run_push(vault)
    assert "new.md" not in git(vault, "ls-files")


def test_a_rejected_push_is_reported_never_forced(vault, tmp_path):
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(tmp_path / "remote.git"), str(other))
    git(other, "config", "user.name", "t")
    git(other, "config", "user.email", "t@example.com")
    (other / "elsewhere.md").write_text("e\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "from elsewhere")
    git(other, "push", "-q")

    (vault / "local.md").write_text("l\n")
    with pytest.raises(RuntimeError, match="push failed"):
        run_push(vault)
    assert remote_log(vault).splitlines()[0] == "from elsewhere"


def test_not_a_repo_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="not a git repository"):
        run_push(tmp_path)


def test_summary_groups_by_top_folder_biggest_first():
    assert summarize(["notion/a.md", "facts/x.md", "notion/b/c.md", "CLAUDE.md"]) == "notion 2, facts 1, CLAUDE.md"


def test_scheduled_daily_last_thing(tmp_path):
    from quartermaster.config import Settings

    settings = Settings(
        vault=tmp_path, claude_cli=None, notion_token=None, discord_bot_token=None, discord_owner_id=None,
        prefs={"digest": {"weekday": "sunday", "hour": 18}},
    )
    task = next(t for t in build_tasks(settings, "daily") if t.name == PUSH_TASK)
    assert task.command[-1] == "push" and task.schedule_args == ["/sc", "daily", "/st", "23:00"]
