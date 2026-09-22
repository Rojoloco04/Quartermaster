"""``qm web``: a local dashboard for seeing what Quartermaster is doing.

Bot status, scheduled jobs, recent agent turns, the live shared conversation
(Discord and terminal), a tailing log, digests, mutes and the user guide. Read
only: nothing here changes state.

The log and transcripts hold email snippets and DMs, so it binds to localhost.
Binding anywhere else (a Tailscale address, say) requires ``QM_WEB_TOKEN``;
open ``/?token=...`` once and a cookie carries it after that.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

from .. import mutes, schedule
from ..agent import transcript_dir
from ..config import REPO_ROOT, Settings

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
        source = "discord" if str(entry.get("entrypoint", "")).startswith("sdk") else "terminal"
        out.append({"role": entry["type"], "text": str(content).strip(),
                    "at": str(entry.get("timestamp", ""))[:19].replace("T", " "), "source": source})
    return files[-1].stem, out[-limit:]


def read_log_from(path: Path, pos: int) -> tuple[int, str]:
    """New log text since byte ``pos`` (a negative pos means "the last -pos bytes")."""
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
    return pos + len(data), data.decode("utf-8", "replace")


def markdown_to_html(text: str) -> str:
    """Enough markdown for the guide: headings, bullets, code blocks, `code`, **bold**."""
    out: list[str] = []
    in_code = in_list = False
    for raw in text.splitlines():
        if raw.startswith("```"):
            out.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(raw))
            continue
        line = html.escape(raw)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", line)
        if in_list and not line.startswith("- "):
            out.append("</ul>")
            in_list = False
        if m := re.match(r"(#{1,4}) (.*)", line):
            out.append(f"<h{len(m[1]) + 1}>{m[2]}</h{len(m[1]) + 1}>")
        elif line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{line[2:]}</li>")
        elif line.strip():
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


# --- Rendering ---------------------------------------------------------------

CSS = """
:root { --bg:#fbfbfa; --fg:#1d1d1b; --muted:#6b6b66; --line:#e3e3df; --card:#fff;
        --ok:#1f7a4d; --bad:#b3261e; --accent:#3a5ccc; --code:#f2f2ef; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161615; --fg:#ececea; --muted:#9a9a94; --line:#2c2c2a; --card:#1e1e1c;
          --ok:#5cc28f; --bad:#f08a80; --accent:#8fa6ff; --code:#262624; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--fg);
  font:14px/1.5 system-ui, sans-serif } a { color:var(--accent) }
header { display:flex; gap:16px; align-items:baseline; padding:14px 20px; border-bottom:1px solid var(--line) }
header h1 { font-size:16px; margin:0 } nav a { margin-right:12px }
main { max-width:1200px; margin:0 auto; padding:16px 20px; display:grid; gap:16px }
section { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 16px; min-width:0; overflow-x:auto }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin:0 0 8px }
table { border-collapse:collapse; width:100% } td, th { text-align:left; padding:4px 8px 4px 0;
  border-bottom:1px solid var(--line); vertical-align:top } th { color:var(--muted); font-weight:500 }
.ok { color:var(--ok) } .bad { color:var(--bad) } .muted { color:var(--muted) }
pre, code { background:var(--code); border-radius:4px; font:12px/1.45 ui-monospace, Consolas, monospace }
pre { padding:10px; overflow:auto; white-space:pre-wrap; word-break:break-word; margin:0 }
#log { height:420px } .msg { padding:6px 0; border-bottom:1px solid var(--line) }
.msg .who { font-weight:600 } .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px }
@media (max-width: 800px) { .grid2 { grid-template-columns:1fr } main { padding:12px } }
"""

LOG_JS = """
let pos = -20000; const el = document.getElementById('log');
async function poll() {
  try {
    const r = await fetch('/api/log?pos=' + pos); const d = await r.json();
    if (d.text) { const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
      el.textContent += d.text; if (el.textContent.length > 400000) el.textContent = el.textContent.slice(-300000);
      if (stick) el.scrollTop = el.scrollHeight; }
    pos = d.pos;
  } catch (e) {}
  setTimeout(poll, 2000);
}
poll();
"""


def _e(value: object) -> str:
    return html.escape(str(value))


def page(title: str, body: str, script: str = "") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{_e(title)}</title>
<style>{CSS}</style></head><body><header><h1>Quartermaster</h1><nav>
<a href="/">Dashboard</a><a href="/guide">Guide</a></nav></header><main>{body}</main>
<script>{script}</script></body></html>"""


def dashboard(settings: Settings) -> str:
    running, detail = bot_status(settings)
    _, log_text = read_log_from(settings.log_path, -TAIL_BYTES)
    session_id, entries = session_entries(transcript_dir(settings.vault))

    tasks = "".join(
        f"<tr><td>{_e(t['name'])}</td><td>{_e(t['last_run'])}</td>"
        f"<td class='{'ok' if t['last_result'] in ('0', '') else 'bad'}'>{_e(t['last_result'])}</td>"
        f"<td>{_e(t['next_run'])}</td></tr>"
        for t in schedule.task_info()
    )
    turns = "".join(
        f"<tr><td>{_e(t['at'])}</td><td>{_e(t['profile'])}</td><td>{_e(t['model'].replace('claude-', ''))}</td>"
        f"<td>{_e(t['prompt'][:140])}</td><td>{t['tools']}</td><td>{_e(t['cost'] and '$' + t['cost'])}</td>"
        f"<td class='{'ok' if t['ok'] else 'bad' if t['ok'] is False else 'muted'}'>"
        f"{'ok' if t['ok'] else 'failed' if t['ok'] is False else 'running'}</td></tr>"
        for t in recent_turns(log_text)
    )
    convo = "".join(
        f"<div class='msg'><span class='who'>{'You' if m['role'] == 'user' else 'Quartermaster'}</span> "
        f"<span class='muted'>{_e(m['at'])} via {m['source']}</span><pre>{_e(m['text'][:3000])}</pre></div>"
        for m in entries
    ) or "<p class='muted'>No shared session yet.</p>"
    digests = sorted(settings.digests_dir.glob("*.md"), reverse=True)[:10] if settings.digests_dir.exists() else []
    muted = mutes.load(settings.muted_file)

    return page("Quartermaster", f"""
<div class="grid2">
<section><h2>Bot</h2><p class="{'ok' if running else 'bad'}"><strong>{'Running' if running else 'Not running'}</strong>
<span class="muted"> {_e(detail)}</span></p>
<p class="muted">Log: {_e(settings.log_path)}<br>Session: {_e(session_id or '-')}
(<code>claude --continue</code> in the vault resumes it)</p></section>
<section><h2>Scheduled jobs</h2><table><tr><th>Task</th><th>Last run</th><th>Result</th><th>Next</th></tr>{tasks}</table></section>
</div>
<section><h2>Recent turns</h2><table><tr><th>When</th><th>Profile</th><th>Model</th><th>Prompt</th>
<th>Tools</th><th>Cost</th><th></th></tr>{turns}</table></section>
<section><h2>Live log</h2><pre id="log"></pre></section>
<section><h2>Conversation (shared by Discord and the terminal)</h2>{convo}</section>
<div class="grid2">
<section><h2>Digests</h2><ul>{''.join(f'<li><a href="/digest/{_e(d.stem)}">{_e(d.stem)}</a></li>' for d in digests) or '<li class="muted">none yet</li>'}</ul></section>
<section><h2>Muted ({len(muted)})</h2><ul>{''.join(f'<li><code>{_e(m.item_id)}</code> <span class="muted">{_e(m.note)}</span></li>' for m in muted) or '<li class="muted">nothing muted</li>'}</ul></section>
</div>""", LOG_JS)


# --- App -----------------------------------------------------------------------


def build_app(settings: Settings, token: str | None = None):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
    from starlette.routing import Route

    def authorised(request: Request) -> bool:
        if token is None:
            return True
        given = request.query_params.get("token") or request.cookies.get("qm_token") or ""
        return secrets.compare_digest(given, token)

    def guarded(handler):
        async def wrapper(request: Request):
            if not authorised(request):
                return PlainTextResponse("Unauthorised. Open /?token=<QM_WEB_TOKEN> once.", status_code=401)
            response = await handler(request)
            if token is not None and request.query_params.get("token"):
                response = RedirectResponse(request.url.path)
                response.set_cookie("qm_token", token, httponly=True, samesite="strict", max_age=30 * 86400)
            return response
        return wrapper

    async def index(request: Request):
        return HTMLResponse(dashboard(settings))

    async def api_log(request: Request):
        pos, text = read_log_from(settings.log_path, int(request.query_params.get("pos", -20000)))
        return JSONResponse({"pos": pos, "text": text})

    async def digest(request: Request):
        name = request.path_params["name"]
        path = settings.digests_dir / f"{name}.md"
        if not re.fullmatch(r"[\w-]+", name) or not path.exists():
            return PlainTextResponse("No such digest.", status_code=404)
        return HTMLResponse(page(f"Digest {name}", f"<section><h2>Digest {_e(name)}</h2><pre>{_e(path.read_text('utf-8'))}</pre></section>"))

    async def guide(request: Request):
        path = REPO_ROOT / "docs" / "GUIDE.md"
        text = path.read_text("utf-8") if path.exists() else "# Guide\n\nNot written yet."
        return HTMLResponse(page("Guide", f"<section>{markdown_to_html(text)}</section>"))

    return Starlette(routes=[
        Route("/", guarded(index)),
        Route("/api/log", guarded(api_log)),
        Route("/digest/{name}", guarded(digest)),
        Route("/guide", guarded(guide)),
    ])


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8766) -> int:
    import uvicorn

    token = os.getenv("QM_WEB_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise RuntimeError(f"Refusing to serve on {host} without QM_WEB_TOKEN set: the log holds DMs and email snippets.")
    uvicorn.run(build_app(settings, token), host=host, port=port, log_level="warning")
    return 0
