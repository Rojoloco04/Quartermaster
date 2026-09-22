"""Windows Task Scheduler wiring for the Notion sync, digest and presale check.

Plain ``schtasks.exe`` - no new dependency, and none of Phase 5's
Docker/service-wrapper work is needed just to get two commands to fire on a
timer (three, now). Runs only while the owner is logged in (no stored credential for
running logged-off - that's a bigger, separate decision).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

SYNC_TASK = "Quartermaster Notion Sync"
TIDY_TASK = "Quartermaster Claude Page Tidy"
DIGEST_TASK = "Quartermaster Digest"
PRESALE_TASK = "Quartermaster Presale Check"
TASKS = (SYNC_TASK, TIDY_TASK, DIGEST_TASK, PRESALE_TASK)

# Before the presale check and the digest, so the stale-page scan reads a
# mirror that is at most a day old.
SYNC_HOUR = 7

# Weekly, Sunday morning: the owner is around to confirm the replace, and it
# lands well before the Sunday evening digest.
TIDY_DAY, TIDY_HOUR = "SUN", 9

# A same-day presale ping only helps if it lands before the ticket window
# it's warning about - early morning, well ahead of typical on-sale times.
PRESALE_HOUR = 8


@dataclass(frozen=True)
class ScheduledTask:
    name: str
    command: list[str]
    schedule_args: list[str]


def _qm_exe() -> str:
    """The venv's qm.exe, resolved from the interpreter running this code -
    the same executable `CLAUDE.md` tells the owner to run by hand."""
    return str(Path(sys.executable).parent / "qm.exe")


def build_tasks(settings: Settings, digest_cadence: str) -> list[ScheduledTask]:
    """Pure: what would be scheduled, without touching the OS. `digest_cadence`
    is a proof-of-concept knob - `daily` for now, `weekly` once the owner is
    happy with what it sends. The presale check is always daily regardless;
    that cadence was never in question, and neither is the daily sync's."""
    if digest_cadence not in ("daily", "weekly"):
        raise ValueError(f"digest_cadence must be 'daily' or 'weekly', got {digest_cadence!r}")

    qm = _qm_exe()
    hour = int(settings.prefs["digest"]["hour"])
    weekday = str(settings.prefs["digest"]["weekday"])[:3].upper()

    if digest_cadence == "weekly":
        digest_schedule = ["/sc", "weekly", "/d", weekday, "/st", f"{hour:02d}:00"]
    else:
        digest_schedule = ["/sc", "daily", "/st", f"{hour:02d}:00"]

    return [
        ScheduledTask(SYNC_TASK, [qm, "sync"], ["/sc", "daily", "/st", f"{SYNC_HOUR:02d}:00"]),
        ScheduledTask(TIDY_TASK, [qm, "tidy"], ["/sc", "weekly", "/d", TIDY_DAY, "/st", f"{TIDY_HOUR:02d}:00"]),
        ScheduledTask(DIGEST_TASK, [qm, "digest"], digest_schedule),
        ScheduledTask(
            PRESALE_TASK, [qm, "presale-check"], ["/sc", "daily", "/st", f"{PRESALE_HOUR:02d}:00"]
        ),
    ]


def install(settings: Settings, digest_cadence: str = "weekly") -> list[str]:
    """Registers every task, overwriting any existing registration (`/f`).
    Returns the commands run, so the caller can show exactly what changed."""
    run: list[str] = []
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
