"""The dashboard (``/``): bot status from its heartbeat, scheduled jobs, recent
agent turns parsed from the log, the shared conversation, the log tail,
digests and mutes. Read-only.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from ..agent import transcript_dir
from ..config import Settings
from ..knowledge import mutes
from ..ops import schedule
from .layout import _e, clip, LOG_JS, page, time_html


HEARTBEAT_STALE = 90  # seconds; the bot writes one every 30
TAIL_BYTES = 2_000_000  # how much of the log the turn table reads

_TURN_START = re.compile(r"^(\S+ \S+) INFO quartermaster\.agent: \[(\w+)\] (\w+) turn start \(model=([^)]+)\): (.*)$")
_TURN_DONE = re.compile(r"\[(\w+)\] turn done: ok=(\w+) cost=\$(\S+)")
_TOOL = re.compile(r"\[(\w+)\] tool call: ([\w-]+)\(")
_STOPPED = re.compile(r"\[(\w+)\] (cancelled|timed out|agent query failed)")


# --- Data (pure, tested) ---------------------------------------------------


def heartbeat_path(settings: Settings) -> Path:
    return settings.log_path.parent / "bot.heartbeat"


def bot_status(settings: Settings, now: float | None = None) -> tuple[bool, str]:
    """(running, description) from the heartbeat the bot writes every 30s."""
    try:
        beat = json.loads(heartbeat_path(settings).read_text("utf-8"))
    except (OSError, ValueError):
        return False, "no heartbeat - the bot has not run since this was added"
    age = (now or time.time()) - float(beat.get("at", 0))
    when = datetime.fromtimestamp(float(beat.get("at", 0))).strftime("%Y-%m-%d %H:%M:%S")
    if age > HEARTBEAT_STALE:
        return False, f"last heartbeat {when} ({int(age // 60)} min ago)"
    return True, f"pid {beat.get('pid')}, heartbeat {int(age)}s ago"


def recent_turns(log_text: str, limit: int = 25) -> list[dict]:
    """Agent turns parsed from the log, newest first."""
    turns: dict[str, dict] = {}
    for line in log_text.splitlines():
        if m := _TURN_START.match(line):
            turns[m[2]] = {"id": m[2], "at": m[1][:19], "profile": m[3], "model": m[4],
                           "prompt": m[5], "tools": 0, "cost": "", "ok": None}
        elif (m := _TOOL.search(line)) and m[1] in turns:
            turns[m[1]]["tools"] += 1
        elif (m := _TURN_DONE.search(line)) and m[1] in turns:
            turns[m[1]].update(ok=m[2] == "True", cost=m[3])
        elif (m := _STOPPED.search(line)) and m[1] in turns:
            turns[m[1]]["ok"] = False
    return list(reversed(list(turns.values())))[:limit]


def session_entries(folder: Path, limit: int = 40) -> tuple[str, list[dict]]:
    """(session id, last text messages) of the newest shared-session transcript."""
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if folder.exists() else []
    if not files:
        return "", []
    out: list[dict] = []
    for line in files[-1].read_text("utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        if not content or not str(content).strip():
            continue  # tool calls and results: the log covers those
        # Both the bot and the web chat run turns through the SDK.
        source = "discord/web" if str(entry.get("entrypoint", "")).startswith("sdk") else "terminal"
        out.append({"role": entry["type"], "text": str(content).strip(),
                    "at": _local(str(entry.get("timestamp", ""))), "source": source})
    return files[-1].stem, out[-limit:]


def _local(stamp: str) -> str:
    """A transcript's UTC timestamp ("2026-10-03T17:37:52.123Z") in local time,
    like the log's; "" if there is none."""
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp[:19].replace("T", " ")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def model_name(model_id: str) -> str:
    """"claude-sonnet-5-5" -> "sonnet 5.5"."""
    family, *version = model_id.removeprefix("claude-").split("-")
    return f"{family} {'.'.join(version)}".strip()


# schtasks.exe is ~0.2s per task; a page refreshing itself every 15s shouldn't
# spawn seven of them each time.
TASKS_MAX_AGE = 60
_tasks_cache: dict = {"at": 0.0, "rows": []}


def task_rows() -> list[dict]:
    if time.monotonic() - _tasks_cache["at"] > TASKS_MAX_AGE:
        _tasks_cache.update(at=time.monotonic(), rows=schedule.task_info())
    return _tasks_cache["rows"]


def read_log_from(path: Path, pos: int, whole_lines: bool = False) -> tuple[int, str]:
    """New log text since byte ``pos`` (a negative pos means "the last -pos bytes").
    ``whole_lines`` holds back a trailing partial line for the next read, so a
    caller filtering by line never sees half of one."""
    if not path.exists():
        return 0, ""
    size = path.stat().st_size
    if pos < 0:
        pos = max(0, size + pos)
    if pos > size:  # rotated underneath us
        pos = 0
    with path.open("rb") as fh:
        fh.seek(pos)
        data = fh.read(TAIL_BYTES)
    if whole_lines and b"\n" in data:
        data = data[: data.rindex(b"\n") + 1]
    return pos + len(data), data.decode("utf-8", "replace")


def service_line(service: dict | None) -> str:
    """How the bot is kept running, from the logon task's row in ``task_info``."""
    if service is None or service["last_run"] == "not scheduled":
        return "Not started at logon: <code>qm schedule install</code> sets that up."
    if service["last_result"] == "running":
        return f"Starts at logon and restarts if it crashes (supervisor up since {time_html(service['last_run'])})."
    return "Starts at logon, but the supervisor isn't running now: <code>qm restart</code> starts it."


def message_html(m: dict) -> str:
    """One message of the shared conversation; a long one clipped."""
    who = "You" if m["role"] == "user" else "Quartermaster"
    return (f"<div class='msg'><span class='who'>{who}</span> "
            f"<span class='muted'>{time_html(m['at'])} via {m['source']}</span>"
            f"{clip(m['text'], 300, f'<pre>{_e(m["text"])}</pre>')}</div>")


def job_result(code: str) -> str:
    """Task Scheduler's last result: 0 is "ok", anything else is a failure code."""
    if code in ("0", ""):
        return "ok" if code else ""
    return "running" if code == "running" else f"failed ({_e(code)})"


def dashboard(settings: Settings) -> str:
    running, detail = bot_status(settings)
    _, log_text = read_log_from(settings.log_path, -TAIL_BYTES)
    session_id, entries = session_entries(transcript_dir(settings.vault))

    info = task_rows()
    # The service keeps the bot running; it isn't a timed job, so it's reported with the bot.
    service = next((t for t in info if t["name"] == schedule.SERVICE_TASK), None)
    tasks = "".join(
        f"<tr><td class='nw'>{_e(t['name'].removeprefix('Quartermaster '))}</td><td class='nw'>{time_html(t['last_run'])}</td>"
        f"<td class='nw {'ok' if t['last_result'] in ('0', '') else 'bad'}'>{job_result(t['last_result'])}</td>"
        f"<td class='nw'>{time_html(t['next_run'])}</td></tr>"
        for t in info if t is not service
    )
    turns = "".join(
        f"<tr><td class='nw'>{time_html(t['at'])}</td><td class='nw'>{_e(t['profile'])}</td>"
        f"<td class='nw'>{_e(model_name(t['model']))}</td><td class='grow'>{clip(t['prompt'], 110)}</td>"
        f"<td class='nw'>{t['tools']}</td><td class='nw'>{_e(t['cost'] and '$' + t['cost'])}</td>"
        f"<td class='nw {'ok' if t['ok'] else 'bad' if t['ok'] is False else 'muted'}'>"
        f"{'ok' if t['ok'] else 'failed' if t['ok'] is False else 'running'}</td></tr>"
        for t in recent_turns(log_text)
    )
    convo = "".join(message_html(m) for m in entries) or "<p class='muted'>No shared session yet.</p>"
    digests = sorted(settings.digests_dir.glob("*.md"), reverse=True)[:10] if settings.digests_dir.exists() else []
    muted = mutes.load(settings.muted_file)

    return page("Quartermaster", f"""
<div class="grid2">
<section id="bot" data-live><h2>Bot</h2><p class="{'ok' if running else 'bad'}"><strong>{'Running' if running else 'Not running'}</strong>
<span class="muted"> {_e(detail)}</span></p>
<p class="muted">{service_line(service)}</p>
<p class="muted">Log: {_e(settings.log_path)}<br>Session: {_e(session_id or '-')}
(<code>claude --continue</code> in the vault resumes it)</p></section>
<section id="jobs" data-live><h2>Scheduled jobs</h2><table><tr><th>Task</th><th>Last run</th><th>Result</th><th>Next</th></tr>{tasks}</table></section>
</div>
<section id="turns" data-live><h2>Recent turns</h2><table><tr><th>When</th><th>Profile</th><th>Model</th><th>Prompt</th>
<th>Tools</th><th>Cost</th><th></th></tr>{turns}</table></section>
<section><h2>Live log</h2><pre id="log"></pre></section>
<section id="convo" data-live><h2>Conversation (shared by Discord and the terminal)</h2>{convo}</section>
<div class="grid2">
<section id="digests" data-live><h2>Digests</h2><ul>{''.join(f'<li><a href="/digest/{_e(d.stem)}">{_e(d.stem)}</a></li>' for d in digests) or '<li class="muted">none yet</li>'}</ul></section>
<section id="muted" data-live><h2>Muted ({len(muted)})</h2><ul>{''.join(f'<li><code>{_e(m.item_id)}</code> <span class="muted">{clip(m.note, 80)}</span></li>' for m in muted) or '<li class="muted">nothing muted</li>'}</ul></section>
</div>""", LOG_JS)
