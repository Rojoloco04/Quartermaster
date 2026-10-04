"""What the owner can run from a DM: restart (outside the bot's own tree) and
the allow-listed qm commands."""

import asyncio
import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from quartermaster import agent, cli
from quartermaster.chat import TurnLock
from quartermaster.config import Settings
from quartermaster.mcp_servers import qm
from quartermaster.ops import procs


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "log_path", property(lambda self: tmp_path / "Logs" / "quartermaster.log"))
    s = Settings(vault=tmp_path / "vault")
    monkeypatch.setattr(qm, "settings", lambda: s)
    return s


def test_the_owner_agent_is_told_it_can_restart():
    assert "restart_quartermaster" in agent.OWNER_LIMITS and "run_qm" in agent.OWNER_LIMITS


def test_restart_from_a_turn_runs_outside_the_bots_tree(settings, monkeypatch):
    launched = []
    monkeypatch.setattr(procs, "launch_outside_jobs", lambda cmd, cwd: launched.append((cmd, cwd)) or 42)
    reply = qm.restart_quartermaster(pull_code=True)
    assert "when this turn ends" in reply
    cmd, cwd = launched[0]
    assert cmd.startswith("cmd.exe /d /c ") and cmd.endswith('> restart.out 2>&1"')
    assert "quartermaster.cli restart --wait-for-turn --dm --pull" in cmd
    assert cwd == settings.log_path.parent


def test_wait_for_turn_waits_while_a_turn_holds_the_lock(tmp_path):
    lock = TurnLock(tmp_path / "turn.lock")
    assert lock.acquire()
    assert procs.wait_for_turn(tmp_path / "turn.lock", timeout=0.2, poll=0.05) is False
    lock.release()
    assert procs.wait_for_turn(tmp_path / "turn.lock", timeout=0.2, poll=0.05) is True


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_pull_code_fast_forwards_and_spots_a_dependency_change(tmp_path):
    origin, here, there = tmp_path / "origin.git", tmp_path / "here", tmp_path / "there"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    for repo in (here, there):
        subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "t")
        _git(repo, "checkout", "-q", "-b", "main")
    (there / "a.py").write_text("x = 1\n")
    _git(there, "add", "-A")
    _git(there, "commit", "-q", "-m", "first")
    _git(there, "push", "-q", "-u", "origin", "main")
    _git(here, "pull", "-q", "origin", "main")
    _git(here, "branch", "-q", "--set-upstream-to=origin/main")
    text, deps = procs.pull_code(here)
    assert "already up to date" in text and deps is False

    (there / "a.py").write_text("x = 2\n")
    _git(there, "commit", "-q", "-am", "code only")
    _git(there, "push", "-q")
    text, deps = procs.pull_code(here)
    assert "Pulled 1 commit(s)" in text and "code only" in text and deps is False

    (there / "pyproject.toml").write_text("[project]\n")
    _git(there, "add", "-A")
    _git(there, "commit", "-q", "-m", "new dependency")
    _git(there, "push", "-q")
    assert procs.pull_code(here)[1] is True

    (here / "a.py").write_text("local edit\n")  # diverged: refused, never merged
    _git(here, "commit", "-q", "-am", "local")
    (there / "a.py").write_text("x = 3\n")
    _git(there, "commit", "-q", "-am", "remote")
    _git(there, "push", "-q")
    with pytest.raises(RuntimeError, match="git pull failed"):
        procs.pull_code(here)


def test_cli_restart_waits_pulls_reinstalls_while_down_and_dms(settings, monkeypatch, capsys):
    order, sent = [], []
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(procs, "wait_for_turn", lambda path: order.append("wait") or True)
    monkeypatch.setattr(procs, "pull_code", lambda repo: order.append("pull") or ("Pulled 1 commit(s)", True))
    monkeypatch.setattr(procs, "reinstall", lambda repo: order.append("install"))

    def restart(out_dir, before_start=None):
        order.append("stop")
        before_start()
        order.append("start")
        return ["serve (pid 5)"], ["serve (pid 6) with bot (pid 7), web (pid 8)"], []

    monkeypatch.setattr(procs, "restart", restart)
    from quartermaster.discord_bot import send
    monkeypatch.setattr(send, "send_dm", lambda s, text: sent.append(text))
    assert cli.main(["restart", "--wait-for-turn", "--pull", "--dm"]) == 0
    assert order == ["wait", "pull", "stop", "install", "start"]
    assert sent[0].startswith("Restarted.") and "Pulled 1 commit(s)" in sent[0] and "reinstalled" in sent[0]


def test_a_failed_pull_still_restarts_the_current_code(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    def bad_pull(repo):
        raise RuntimeError("git pull failed: not possible to fast-forward")

    monkeypatch.setattr(procs, "pull_code", bad_pull)
    monkeypatch.setattr(procs, "restart", lambda out_dir, before_start=None: (["serve (pid 5)"], ["serve (pid 6)"], []))
    assert cli.main(["restart", "--pull"]) == 0
    assert "Not updated, restarting the current code" in capsys.readouterr().out


def test_run_qm_refuses_anything_off_the_list(settings):
    for command in ("quit", "auth google personal", "minecraft op Steve", "push; quit", "init --force", "digest"):
        with pytest.raises(ToolError, match="Not allowed"):
            asyncio.run(qm.run_qm(command))


def test_run_qm_runs_a_listed_command_and_reports_failure(settings, monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, stdout="Vault push failed: no upstream\n", stderr="log line\n")

    monkeypatch.setattr(qm.subprocess, "run", fake_run)
    out = asyncio.run(qm.run_qm("qm push"))
    assert calls[0][1:] == ["-m", "quartermaster.cli", "push"]
    assert "no upstream" in out and "Exit code 1" in out and "log line" in out


def test_run_qm_starts_the_test_digest_in_the_background(settings, monkeypatch):
    launched = []
    monkeypatch.setattr(procs, "launch_outside_jobs", lambda cmd, cwd: launched.append(cmd) or 7)
    assert "own DM" in asyncio.run(qm.run_qm("digest --test"))
    assert "quartermaster.cli digest --test" in launched[0]
