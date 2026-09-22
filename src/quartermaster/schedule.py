"""Windows Task Scheduler wiring: the timed jobs, and the service.

Plain ``schtasks.exe``, no new dependency. Everything runs only while the owner
is logged in (no stored credential for running logged-off): the Claude login
and the OAuth tokens live in the owner's profile anyway.

The service is a logon task running ``qm serve`` (the supervisor in
``procs``) through pythonw, so there is no console window. It is registered
from XML because ``schtasks /create`` flags can't express what it needs: no
execution time limit (the default kills a task after 72 hours), keep running
on battery, and ignore a second start while one is running.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .config import REPO_ROOT, Settings

SYNC_TASK = "Quartermaster Notion Sync"
TIDY_TASK = "Quartermaster Claude Page Tidy"
DIGEST_TASK = "Quartermaster Digest"
PRESALE_TASK = "Quartermaster Presale Check"
RECONCILE_TASK = "Quartermaster Reconcile"
PUSH_TASK = "Quartermaster Vault Push"
SERVICE_TASK = "Quartermaster Service"
TASKS = (SERVICE_TASK, SYNC_TASK, TIDY_TASK, DIGEST_TASK, PRESALE_TASK, RECONCILE_TASK, PUSH_TASK)

# Before the presale check and the digest, so the stale-page scan reads a
# mirror that is at most a day old.
SYNC_HOUR = 7

# Weekly, Sunday morning: the owner is around to confirm the replace, and it
# lands well before the Sunday evening digest.
TIDY_DAY, TIDY_HOUR = "SUN", 9

# A same-day presale ping only helps if it lands before the ticket window
# it's warning about - early morning, well ahead of typical on-sale times.
PRESALE_HOUR = 8

# After the sync (so it compares facts with a fresh mirror) and before the
# digest, so the digest reads reconciled facts.
RECONCILE_TIME = "07:30"

# The git remote is the vault's only backup. Last thing in the day, after the
# digest, so one commit carries the day's changes. The PC stays on, so a fixed
# time is fine; a missed run is picked up by the next one.
PUSH_TIME = "23:00"


@dataclass(frozen=True)
class ScheduledTask:
    name: str
    command: list[str]
    schedule_args: list[str]


def _job(subcommand: str) -> list[str]:
    """A timed job's command: the venv's pythonw, so no window opens (qm.exe is
    a console program, and each run opened a Windows Terminal). ``cli.main``
    re-runs itself with a hidden console for the programs a job starts."""
    return [str(Path(sys.executable).with_name("pythonw.exe")), "-m", "quartermaster.cli", subcommand]


def build_tasks(settings: Settings, digest_cadence: str) -> list[ScheduledTask]:
    """Pure: what would be scheduled, without touching the OS. `digest_cadence`
    is a proof-of-concept knob - `daily` for now, `weekly` once the owner is
    happy with what it sends. The presale check is always daily regardless;
    that cadence was never in question, and neither is the daily sync's."""
    if digest_cadence not in ("daily", "weekly"):
        raise ValueError(f"digest_cadence must be 'daily' or 'weekly', got {digest_cadence!r}")

    hour = int(settings.prefs["digest"]["hour"])
    weekday = str(settings.prefs["digest"]["weekday"])[:3].upper()

    if digest_cadence == "weekly":
        digest_schedule = ["/sc", "weekly", "/d", weekday, "/st", f"{hour:02d}:00"]
    else:
        digest_schedule = ["/sc", "daily", "/st", f"{hour:02d}:00"]

    return [
        ScheduledTask(SYNC_TASK, _job("sync"), ["/sc", "daily", "/st", f"{SYNC_HOUR:02d}:00"]),
        ScheduledTask(TIDY_TASK, _job("tidy"), ["/sc", "weekly", "/d", TIDY_DAY, "/st", f"{TIDY_HOUR:02d}:00"]),
        ScheduledTask(DIGEST_TASK, _job("digest"), digest_schedule),
        ScheduledTask(
            PRESALE_TASK, _job("presale-check"), ["/sc", "daily", "/st", f"{PRESALE_HOUR:02d}:00"]
        ),
        ScheduledTask(RECONCILE_TASK, _job("reconcile"), ["/sc", "daily", "/st", RECONCILE_TIME]),
        ScheduledTask(PUSH_TASK, _job("push"), ["/sc", "daily", "/st", PUSH_TIME]),
    ]


def _user() -> str:
    domain, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    return f"{domain}\\{name}" if domain else name


def service_xml(user: str, pythonw: str, workdir: str) -> str:
    """The logon task that keeps the bot and dashboard running. Pure, for tests."""
    u, exe, cwd = escape(user), escape(pythonw), escape(workdir)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Quartermaster: runs the Discord bot and the dashboard (qm serve) and restarts them if they exit.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{u}</UserId><Delay>PT30S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{u}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
  </Settings>
  <Actions Context="Author"><Exec><Command>{exe}</Command><Arguments>-m quartermaster.cli serve</Arguments><WorkingDirectory>{cwd}</WorkingDirectory></Exec></Actions>
</Task>
"""


def install_service() -> str:
    xml = service_xml(_user(), str(Path(sys.executable).with_name("pythonw.exe")), str(REPO_ROOT))
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(path).write_text(xml, encoding="utf-16")  # schtasks wants UTF-16 with a BOM
        cmd = ["schtasks", "/create", "/f", "/tn", SERVICE_TASK, "/xml", path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Couldn't register {SERVICE_TASK}: {(result.stderr or result.stdout).strip()}")
    finally:
        os.unlink(path)
    return f"schtasks /create /f /tn \"{SERVICE_TASK}\" /xml <logon task: qm serve>"


def service_installed() -> bool:
    return subprocess.run(["schtasks", "/query", "/tn", SERVICE_TASK], capture_output=True, text=True).returncode == 0


def run_service() -> None:
    subprocess.run(["schtasks", "/run", "/tn", SERVICE_TASK], capture_output=True, text=True, check=True)


def install(settings: Settings, digest_cadence: str = "weekly") -> list[str]:
    """Registers every task, overwriting any existing registration (`/f`).
    Returns the commands run, so the caller can show exactly what changed."""
    run: list[str] = [install_service()]
    for task in build_tasks(settings, digest_cadence):
        cmd = [
            "schtasks", "/create", "/f",
            "/tn", task.name,
            "/tr", subprocess.list2cmdline(task.command),
            *task.schedule_args,
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        run.append(" ".join(cmd))
    return run


def remove() -> list[str]:
    run: list[str] = []
    for name in TASKS:
        cmd = ["schtasks", "/delete", "/tn", name, "/f"]
        subprocess.run(cmd, capture_output=True, text=True)  # "task not found" is fine here
        run.append(" ".join(cmd))
    return run


def task_info() -> list[dict]:
    """Name, last run, last result and next run of each task, for the dashboard.
    A task that isn't registered comes back with "not scheduled"."""
    wanted = {"Last Run Time": "last_run", "Last Result": "last_result", "Next Run Time": "next_run"}
    rows = []
    for name in TASKS:
        row = {"name": name, "last_run": "not scheduled", "last_result": "", "next_run": ""}
        result = subprocess.run(
            ["schtasks", "/query", "/tn", name, "/fo", "list", "/v"], capture_output=True, text=True
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                key, _, value = line.partition(":")
                if key.strip() in wanted:
                    row[wanted[key.strip()]] = value.strip()
            if row["last_result"] == "267011":  # SCHED_S_TASK_HAS_NOT_RUN, with a 1999 placeholder date
                row.update(last_run="never", last_result="")
            elif row["last_result"] == "267009":  # SCHED_S_TASK_RUNNING: the service, normally
                row["last_result"] = "running"
        rows.append(row)
    return rows


def status() -> str:
    parts: list[str] = []
    for name in TASKS:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", name, "/fo", "list", "/v"],
            capture_output=True, text=True,
        )
        parts.append(result.stdout.strip() if result.returncode == 0 else f"{name}: not scheduled")
    return "\n\n".join(parts)
