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

from ..config import REPO_ROOT, Settings


SYNC_TASK = "Quartermaster Notion Sync"
TIDY_TASK = "Quartermaster Claude Page Tidy"
DIGEST_TASK = "Quartermaster Digest"
RECONCILE_TASK = "Quartermaster Reconcile"
PUSH_TASK = "Quartermaster Vault Push"
BACKUP_TASK = "Quartermaster Server Backup"
SERVICE_TASK = "Quartermaster Service"
TASKS = (SERVICE_TASK, SYNC_TASK, TIDY_TASK, DIGEST_TASK, RECONCILE_TASK, PUSH_TASK, BACKUP_TASK)
# Tasks earlier versions registered: deleted by install and remove. The
# presale check was folded into the daily digest on 2026-10-03.
RETIRED_TASKS = ("Quartermaster Presale Check",)

# The chores, one after another between 03:00 and 04:00. The three that call
# the model (reconcile, tidy, the digest) then spend a 5-hour usage window that
# closes by 08:00, before the owner's day starts. Their DMs (the digest,
# reconcile's questions, tidy's Confirm) are held and the bot delivers them at
# ``digest.hour`` (``discord_bot.held``). Five minutes apart: in the log the
# sync takes ~10s, reconcile ~45s, the digest ~2 min. The PC stays on, so
# fixed times are fine; a missed run is picked up by the next one.
#
# First the Notion sync: reconcile compares facts with the mirror and the
# digest's stale-page scan reads it.
SYNC_TIME = "03:00"
# After the sync, before the digest, so the digest reads reconciled facts.
RECONCILE_TIME = "03:05"
# Weekly. Its proposal is held for the morning like the DMs.
TIDY_DAY, TIDY_TIME = "SUN", "03:10"
# Built and archived now, delivered by the bot at digest.hour.
DIGEST_TIME = "03:15"
# The git remote is the vault's only backup. After the others, so one commit
# carries the previous day plus this morning's reconcile and digest.
PUSH_TIME = "03:30"
# The game servers' zips to the backup drive, when friends are least likely to
# be on (a running Minecraft server pauses autosave for the copy).
BACKUP_TIME = "03:35"


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
    happy with what it sends. The digest is built at ``DIGEST_TIME`` and
    delivered by the bot at ``digest.hour`` (08:00: on-sales are in it, and
    must land before the ticket windows open)."""
    if digest_cadence not in ("daily", "weekly"):
        raise ValueError(f"digest_cadence must be 'daily' or 'weekly', got {digest_cadence!r}")

    weekday = str(settings.prefs["digest"]["weekday"])[:3].upper()

    if digest_cadence == "weekly":
        digest_schedule = ["/sc", "weekly", "/d", weekday, "/st", DIGEST_TIME]
    else:
        digest_schedule = ["/sc", "daily", "/st", DIGEST_TIME]

    return [
        ScheduledTask(SYNC_TASK, _job("sync"), ["/sc", "daily", "/st", SYNC_TIME]),
        ScheduledTask(TIDY_TASK, _job("tidy"), ["/sc", "weekly", "/d", TIDY_DAY, "/st", TIDY_TIME]),
        ScheduledTask(DIGEST_TASK, _job("digest"), digest_schedule),
        ScheduledTask(RECONCILE_TASK, _job("reconcile"), ["/sc", "daily", "/st", RECONCILE_TIME]),
        ScheduledTask(PUSH_TASK, _job("push"), ["/sc", "daily", "/st", PUSH_TIME]),
        ScheduledTask(BACKUP_TASK, _job("backup"), ["/sc", "daily", "/st", BACKUP_TIME]),
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
    return run + _delete(RETIRED_TASKS, only_existing=True)


def _delete(names, only_existing: bool = False) -> list[str]:
    run: list[str] = []
    for name in names:
        if only_existing and subprocess.run(["schtasks", "/query", "/tn", name], capture_output=True).returncode:
            continue
        cmd = ["schtasks", "/delete", "/tn", name, "/f"]
        subprocess.run(cmd, capture_output=True, text=True)  # "task not found" is fine here
        run.append(" ".join(cmd))
    return run


def remove() -> list[str]:
    return _delete(TASKS + RETIRED_TASKS)


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
