"""``qm quit``: stop every running Quartermaster process in one command.

Restarting by hand used to leave a second bot connected (it double-replies),
and ``Get-Process qm | Stop-Process`` missed the MCP servers and Claude CLI
children, which run as python.exe / claude.exe. This finds every qm.exe, and
every python process running ``quartermaster``, then kills each tree
(``taskkill /T``) so their claude.exe children go too. The command's own
process and its parents are never touched.

``qm restart`` stops only the bot and the dashboard (a scheduled job mid-run
is left alone), then starts both again detached from the terminal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


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


def describe(proc: dict) -> str:
    return f"{command(proc) or proc['name']} (pid {proc['pid']})"


def quit_all(only: set[str] | None = None) -> list[str]:
    """Kill every Quartermaster process tree, or only those running one of the
    subcommands in ``only``. Returns what was stopped."""
    stopped = []
    for proc in targets(list_processes(), os.getpid()):
        if only is not None and command(proc) not in only:
            continue
        result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc["pid"])], capture_output=True, text=True)
        if result.returncode == 0:
            stopped.append(describe(proc))
    return stopped


RESTARTED = ("bot", "web")
STARTUP_GRACE = 4  # seconds a restarted process must survive to count as started


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


def restart(out_dir: Path) -> tuple[list[str], list[str], list[str]]:
    """Stop the bot and dashboard, start both again. Returns (stopped,
    started, failed); a failed entry carries the tail of its output file."""
    stopped = quit_all(set(RESTARTED))
    running = {name: start_detached(name, out_dir / f"{name}.out") for name in RESTARTED}
    time.sleep(STARTUP_GRACE)
    started, failed = [], []
    for name, proc in running.items():
        if proc.poll() is None:
            started.append(f"{name} (pid {proc.pid})")
        else:
            tail = (out_dir / f"{name}.out").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-5:]
            failed.append(f"{name} exited with {proc.returncode}:" + "".join(f"\n    {line}" for line in tail or ["(no output)"]))
    return stopped, started, failed
