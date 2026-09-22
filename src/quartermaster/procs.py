"""``qm quit``: stop every running Quartermaster process in one command.

Restarting by hand used to leave a second bot connected (it double-replies),
and ``Get-Process qm | Stop-Process`` missed the MCP servers and Claude CLI
children, which run as python.exe / claude.exe. This finds every qm.exe, and
every python process running ``quartermaster``, then kills each tree
(``taskkill /T``) so their claude.exe children go too. The command's own
process and its parents are never touched.
"""

from __future__ import annotations

import json
import os
import subprocess


def list_processes() -> list[dict]:
    """[{pid, ppid, name, cmd}] for every process, via CIM (no psutil needed)."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, text=True, check=True,
    ).stdout
    rows = json.loads(out or "[]")
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


def describe(proc: dict) -> str:
    cmd = proc["cmd"]
    for word in ("bot", "web", "digest", "presale-check", "sync", "tidy", "reconcile", "mcp"):
        if f" {word}" in cmd:
            return f"{word} (pid {proc['pid']})"
    return f"{proc['name']} (pid {proc['pid']})"


def quit_all() -> list[str]:
    """Kill every Quartermaster process tree. Returns what was stopped."""
    stopped = []
    for proc in targets(list_processes(), os.getpid()):
        result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc["pid"])], capture_output=True, text=True)
        if result.returncode == 0:
            stopped.append(describe(proc))
    return stopped
