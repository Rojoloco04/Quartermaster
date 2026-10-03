"""``qm quit``: stop every running Quartermaster process in one command.

Restarting by hand used to leave a second bot connected (it double-replies),
and ``Get-Process qm | Stop-Process`` missed the MCP servers and Claude CLI
children, which run as python.exe / claude.exe. This finds every qm.exe, and
every python process running ``quartermaster``, then kills each tree
(``taskkill /T``) so their claude.exe children go too. The command's own
process and its parents are never touched.

The bot and the dashboard always run one way: as children of ``qm serve``,
a supervisor that restarts either one when it exits (backing off while one
keeps crashing). Task Scheduler only starts a process, it doesn't keep one
alive, so something has to. The ``Quartermaster Service`` logon task starts
it windowless (pythonw). ``qm restart`` stops that tree (a scheduled job
mid-run is left alone) and starts it again: through the logon task when
registered, else detached from the terminal. ``qm bot`` / ``qm web`` by hand
are for debugging in the foreground; each holds an OS lock for its whole run,
so a second copy refuses to start however it was launched: two connected
bots double-reply.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


log = logging.getLogger(__name__)


def list_processes() -> list[dict]:
    """[{pid, ppid, name, cmd}] for every process, via CIM (no psutil needed)."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, text=True, check=True,
    ).stdout
    # strict=False: a command line can hold raw control characters (a
    # multi-line prompt passed to claude.exe), which PowerShell leaves unescaped.
    rows = json.loads(out or "[]", strict=False)
    rows = rows if isinstance(rows, list) else [rows]
    return [{"pid": r["ProcessId"], "ppid": r["ParentProcessId"], "name": (r["Name"] or "").lower(),
             "cmd": r["CommandLine"] or ""} for r in rows]


def ours(proc: dict) -> bool:
    """Running Quartermaster, not merely living in its venv: VS Code's language
    servers run on the venv's python too, and an early version killed them."""
    if proc["name"] == "qm.exe":
        return True
    cmd = proc["cmd"].lower().replace("\\", "/")
    return proc["name"].startswith("python") and ("/qm.exe" in cmd or "quartermaster.cli" in cmd)


def targets(procs: list[dict], self_pid: int) -> list[dict]:
    """Quartermaster processes to kill: not this one or its ancestors, and not
    one whose parent is already a target (killing the tree covers it)."""
    by_pid = {p["pid"]: p for p in procs}
    spare, pid = set(), self_pid
    while pid in by_pid and pid not in spare:
        spare.add(pid)
        pid = by_pid[pid]["ppid"]
    hits = {p["pid"] for p in procs if ours(p) and p["pid"] not in spare}
    return [p for p in procs if p["pid"] in hits and p["ppid"] not in hits]


def command(proc: dict) -> str | None:
    """The qm subcommand a process is running (``bot``, ``web``, ...), if any."""
    words = proc["cmd"].replace('"', " ").split()
    for i, word in enumerate(words[:-1]):
        w = word.lower().replace("\\", "/")
        if w in ("qm", "qm.exe", "quartermaster.cli") or w.endswith(("/qm", "/qm.exe")):
            return words[i + 1].lower()
    return None


def describe(proc: dict, procs: list[dict] = ()) -> str:
    """``bot (pid 10)``, or for the supervisor the children that go with it:
    ``serve (pid 5) with bot (pid 10), web (pid 20)``. qm.exe is a launcher
    whose python child runs the same subcommand; the outer one is named."""
    text = f"{command(proc) or proc['name']} (pid {proc['pid']})"
    kids, seen, queue = [], {command(proc)}, [proc["pid"]]
    while queue:
        parent = queue.pop(0)
        for p in procs:
            if p["ppid"] == parent:
                queue.append(p["pid"])
                if ours(p) and (name := command(p)) and name not in seen:
                    seen.add(name)
                    kids.append(f"{name} (pid {p['pid']})")
    return text + (" with " + ", ".join(kids) if kids else "")


def quit_all(only: set[str] | None = None) -> list[str]:
    """Kill every Quartermaster process tree, or only those running one of the
    subcommands in ``only``. Returns what was stopped."""
    stopped = []
    procs = list_processes()
    for proc in targets(procs, os.getpid()):
        if only is not None and command(proc) not in only:
            continue
        result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc["pid"])], capture_output=True, text=True)
        if result.returncode == 0:
            stopped.append(describe(proc, procs))
    return stopped


RESTARTED = ("bot", "web")
SERVICE_GRACE = 20  # seconds to wait for serve to bring bot and web up


def instance_lock(lock_dir: Path, name: str):
    """Hold ``<name>.lock`` for this process's lifetime, or None if another
    process already does. An OS lock: a process that dies releases it."""
    from ..chat import TurnLock  # the same OS file lock, for a different job

    lock = TurnLock(lock_dir / f"{name}.lock")
    return lock if lock.acquire() else None


def _qm() -> list[str]:
    exe = Path(sys.executable).with_name("qm.exe")
    return [str(exe)] if exe.exists() else [sys.executable, "-m", "quartermaster.cli"]


def start_detached(subcommand: str, out_path: Path) -> subprocess.Popen:
    """Start ``qm <subcommand>`` with no console, surviving this terminal.
    Its stdout/stderr go to ``out_path`` (a crash before logging is set up
    would otherwise vanish)."""
    # CREATE_NO_WINDOW, not DETACHED_PROCESS: qm.exe is a launcher that starts
    # python.exe, and a console child of a console-less parent gets a fresh
    # (visible, blank) console window. This gives both a hidden one to share.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as out:
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        try:
            # Break out of the terminal's job object, or closing VS Code's
            # terminal takes the bot with it.
            return subprocess.Popen(_qm() + [subcommand], creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except PermissionError:  # the job forbids breakaway
            return subprocess.Popen(_qm() + [subcommand], creationflags=flags, **kwargs)


# Win32_Process.Create: the new process is made by the WMI service, so it
# belongs to no job of ours. Popen with CREATE_BREAKAWAY_FROM_JOB wasn't
# enough: the first live Minecraft start died with the owner turn that ran the
# tool. Game servers start this way.
_WMI_CREATE = (
    "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
    "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
    "@{CommandLine=$env:QM_CMDLINE; CurrentDirectory=$env:QM_CWD; ProcessStartupInformation=$si}; "
    "\"$($r.ReturnValue) $($r.ProcessId)\""
)


def launch_outside_jobs(cmdline: str, cwd: Path) -> int:
    """Start ``cmdline`` hidden, parented by WMI, and return its pid. Nothing of
    ours (a turn, ``qm quit``, a closed terminal) takes it down."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WMI_CREATE],
        capture_output=True, text=True, env={**os.environ, "QM_CMDLINE": cmdline, "QM_CWD": str(cwd)},
    )
    code, _, pid = out.stdout.strip().partition(" ")
    if out.returncode != 0 or code != "0" or not pid.isdigit():
        raise RuntimeError(f"Windows wouldn't start the server: {(out.stderr or out.stdout).strip()[:300]}")
    return int(pid)


def _tail(path: Path, lines: int = 5) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-lines:]
    except OSError:
        return []


def start_child(subcommand: str, out_path: Path) -> subprocess.Popen:
    """``qm <subcommand>`` as the supervisor's child: no console, and not broken
    away from its job, so ending the supervisor ends it too."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as out:
        return subprocess.Popen(_qm() + [subcommand], creationflags=subprocess.CREATE_NO_WINDOW,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)


class Supervisor:
    """Keeps each child running. One that exits is started again after a delay
    that doubles while it keeps dying young, and resets once it has run
    ``HEALTHY`` seconds. Clock and starter are injectable for tests."""

    FIRST_DELAY = 5
    MAX_DELAY = 300
    HEALTHY = 600

    def __init__(self, names: tuple[str, ...], out_dir: Path,
                 start: Callable[[str, Path], subprocess.Popen] = start_child,
                 clock: Callable[[], float] = time.monotonic):
        self.names, self.out_dir, self._start, self._clock = names, out_dir, start, clock
        self.procs: dict[str, subprocess.Popen | None] = {n: None for n in names}
        self.started: dict[str, float] = {}
        self.due: dict[str, float] = {n: 0.0 for n in names}
        self.delay: dict[str, float] = {n: 0.0 for n in names}

    def step(self) -> None:
        now = self._clock()
        for name in self.names:
            proc = self.procs[name]
            if proc is not None:
                code = proc.poll()
                if code is None:
                    if now - self.started[name] >= self.HEALTHY:
                        self.delay[name] = 0.0
                    continue
                ran = now - self.started[name]
                wait = self.FIRST_DELAY if ran >= self.HEALTHY else min(self.MAX_DELAY, max(self.FIRST_DELAY, self.delay[name] * 2))
                self.delay[name], self.due[name], self.procs[name] = wait, now + wait, None
                log.warning("%s exited with %s after %.0fs; restarting in %.0fs. Last output: %s",
                            name, code, ran, wait, " | ".join(_tail(self.out_dir / f"{name}.out")) or "(none)")
            if self.procs[name] is None and now >= self.due[name]:
                self.procs[name] = self._start(name, self.out_dir / f"{name}.out")
                self.started[name] = now
                log.info("supervisor started %s (pid %s)", name, self.procs[name].pid)

    def run(self, poll: float = 2.0) -> None:
        while True:
            self.step()
            time.sleep(poll)


def _running(names: tuple[str, ...]) -> dict[str, int]:
    """Subcommand -> pid, the outermost of a launcher and its python child
    (the pid ``describe`` shows)."""
    found = [p for p in list_processes() if ours(p) and command(p) in names]
    by_pid = {p["pid"]: p for p in found}
    return {command(p): p["pid"] for p in found
            if not (p["ppid"] in by_pid and command(by_pid[p["ppid"]]) == command(p))}


def restart(out_dir: Path) -> tuple[list[str], list[str], list[str]]:
    """Stop serve with its bot and dashboard (and a stray hand-started bot or
    web), then start serve again. Returns (stopped, started, failed); a failed
    entry carries the tail of its output file. Through the logon task when it
    is registered, else ``qm serve`` detached from this terminal."""
    from . import schedule

    stopped = quit_all({"serve", *RESTARTED})
    if schedule.service_installed():
        schedule.run_service()
    else:
        start_detached("serve", out_dir / "serve.out")
    # The task, pythonw and the launchers take a few seconds to reach the children.
    wanted = ("serve", *RESTARTED)
    deadline = time.monotonic() + SERVICE_GRACE
    while len(up := _running(wanted)) < len(wanted) and time.monotonic() < deadline:
        time.sleep(1)
    started = []
    if "serve" in up:
        kids = [f"{name} (pid {up[name]})" for name in RESTARTED if name in up]
        started.append(f"serve (pid {up['serve']})" + (" with " + ", ".join(kids) if kids else ""))
    failed = [f"{name} isn't running:" + "".join(f"\n    {line}" for line in _tail(out_dir / f"{name}.out") or ["(no output)"])
              for name in wanted if name not in up]
    return stopped, started, failed
