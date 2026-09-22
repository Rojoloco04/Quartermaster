from quartermaster import procs


def _p(pid, ppid, name, cmd):
    return {"pid": pid, "ppid": ppid, "name": name, "cmd": cmd}


def test_command_reads_the_subcommand_after_qm():
    assert procs.command(_p(1, 0, "qm.exe", r'"C:\Program Files\q\.venv\Scripts\qm.exe" bot')) == "bot"
    assert procs.command(_p(1, 0, "qm.exe", "qm  web --port 8766")) == "web"
    assert procs.command(_p(1, 0, "python.exe", r"C:\q\.venv\Scripts\python.exe -m quartermaster.cli mcp google")) == "mcp"
    assert procs.command(_p(1, 0, "python.exe", r"C:\q\.venv\Scripts\python.exe some_server.py")) is None


def test_restart_targets_only_bot_and_web(monkeypatch):
    rows = [
        _p(10, 1, "qm.exe", r"C:\v\qm.exe bot"),
        _p(11, 10, "claude.exe", "claude.exe --output-format stream-json"),
        _p(20, 1, "qm.exe", r"C:\v\qm.exe web"),
        _p(30, 1, "qm.exe", r"C:\v\qm.exe digest"),
        _p(40, 1, "python.exe", r"C:\v\python.exe C:\v\qm.exe mcp google"),
        _p(99, 1, "qm.exe", r"C:\v\qm.exe restart"),
    ]
    killed = []
    monkeypatch.setattr(procs, "list_processes", lambda: rows)
    monkeypatch.setattr(procs.os, "getpid", lambda: 99)
    monkeypatch.setattr(procs.subprocess, "run",
                        lambda cmd, **kw: killed.append(int(cmd[-1])) or type("R", (), {"returncode": 0})())
    assert procs.quit_all({"bot", "web"}) == ["bot (pid 10)", "web (pid 20)"]
    assert killed == [10, 20]


def test_quit_all_spares_itself_and_kills_the_rest(monkeypatch):
    rows = [_p(10, 1, "qm.exe", r"C:\v\qm.exe bot"), _p(30, 1, "qm.exe", r"C:\v\qm.exe digest"),
            _p(99, 1, "qm.exe", r"C:\v\qm.exe quit")]
    monkeypatch.setattr(procs, "list_processes", lambda: rows)
    monkeypatch.setattr(procs.os, "getpid", lambda: 99)
    monkeypatch.setattr(procs.subprocess, "run", lambda cmd, **kw: type("R", (), {"returncode": 0})())
    assert procs.quit_all() == ["bot (pid 10)", "digest (pid 30)"]
