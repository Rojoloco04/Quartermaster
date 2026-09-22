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


def test_the_windowless_supervisor_counts_as_ours():
    serve = _p(5, 1, "pythonw.exe", r"C:\v\pythonw.exe -m quartermaster.cli serve")
    assert procs.ours(serve) and procs.command(serve) == "serve"


def test_a_second_instance_is_refused(tmp_path):
    first = procs.instance_lock(tmp_path, "bot")
    assert first is not None
    assert procs.instance_lock(tmp_path, "bot") is None
    assert procs.instance_lock(tmp_path, "web") is not None  # a different name is its own lock
    first.release()
    assert procs.instance_lock(tmp_path, "bot") is not None


class FakeProc:
    pids = iter(range(100, 1000))

    def __init__(self):
        self.pid, self.code = next(FakeProc.pids), None

    def poll(self):
        return self.code


class TestSupervisor:
    def make(self, tmp_path):
        self.now, self.started = 0.0, []

        def start(name, out):
            proc = FakeProc()
            self.started.append((name, self.now))
            return proc

        return procs.Supervisor(("bot", "web"), tmp_path, start=start, clock=lambda: self.now)

    def test_starts_both_and_leaves_running_ones_alone(self, tmp_path):
        sup = self.make(tmp_path)
        sup.step()
        self.now = 50
        sup.step()
        assert self.started == [("bot", 0.0), ("web", 0.0)]

    def test_a_crash_loop_backs_off_and_a_healthy_run_resets(self, tmp_path):
        sup = self.make(tmp_path)
        sup.step()
        waits = []
        for _ in range(4):  # the bot dies young every time
            sup.procs["bot"].code = 1
            sup.step()
            waits.append(sup.due["bot"] - self.now)
            self.now = sup.due["bot"] - 1
            sup.step()
            assert sup.procs["bot"] is None  # not before it's due
            self.now = sup.due["bot"]
            sup.step()
        assert waits == [5, 10, 20, 40]
        assert [n for n, _ in self.started].count("web") == 1  # web untouched throughout

        self.now += procs.Supervisor.HEALTHY + 1  # runs long enough to count as healthy
        sup.step()
        sup.procs["bot"].code = 0
        sup.step()
        assert sup.due["bot"] - self.now == 5

    def test_backoff_is_capped(self, tmp_path):
        sup = self.make(tmp_path)
        sup.step()
        for _ in range(12):
            sup.procs["bot"].code = 1
            sup.step()
            self.now = sup.due["bot"]
            sup.step()
        assert sup.delay["bot"] == procs.Supervisor.MAX_DELAY
