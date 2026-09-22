"""``qm web``: a local dashboard for seeing what Quartermaster is doing.

Bot status, scheduled jobs, recent agent turns, the live shared conversation
(Discord and terminal), a tailing log, digests, mutes, the vault drawn as a graph
(/brain), the user guide, and /settings: every preference, the agent's
instructions, facts, lessons, mutes and the dev queue, viewable and editable.
And /chat: the owner's conversation, the same session as the Discord DMs.
Those edits and chat turns are the only things here that change state.

The log and transcripts hold email snippets and DMs, so it binds to localhost.
Binding anywhere else (a Tailscale address, say) requires ``QM_WEB_TOKEN``;
open ``/?token=...`` once and a cookie carries it after that. Requests must
name an expected Host (a DNS-rebinding page can't read or write through the
owner's browser), and a save must carry the per-run CSRF token from the page.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import secrets
import time
import tomllib
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .. import agent, mutes, schedule
from ..agent import transcript_dir
from ..config import DEFAULTS, REPO_ROOT, Settings, _deep_merge
from . import brain, chat

log = logging.getLogger(__name__)

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


# --- Editing (the only writes qm web makes) ------------------------------------
#
# Everything that configures Quartermaster or holds what it knows, so the owner
# never has to open the vault to check or change it. Not the Notion mirror (the
# next sync overwrites it; edit in Notion), and not digests or the inbox.

EDITABLE = ("CLAUDE.md", "90-System/config.toml", "90-System/muted.md", "90-System/dev-queue.md",
            "90-System/conflicts.md")
_FACT = re.compile(r"facts/[\w.-]+\.md")


class EditRefused(ValueError):
    pass


def editable_path(vault: Path, rel: str) -> Path | None:
    rel = rel.replace("\\", "/")
    if rel not in EDITABLE and not _FACT.fullmatch(rel):
        return None
    path = (vault / rel).resolve()
    return path if vault.resolve() in path.parents else None


def file_hash(path: Path) -> str:
    """What the editor loaded, so a save can tell if the agent wrote in between."""
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def save_file(vault: Path, rel: str, text: str, loaded_hash: str) -> str:
    """Write an editable file; returns its new hash. Refuses a path outside
    ``EDITABLE``, a file changed since it was loaded, and TOML that won't parse."""
    path = editable_path(vault, rel)
    if path is None:
        raise EditRefused(f"{rel} isn't editable here.")
    if file_hash(path) != loaded_hash:
        raise EditRefused("It changed since you opened it (the agent may have written to it). Reload and redo your edit.")
    text = text.replace("\r\n", "\n").rstrip("\n") + "\n"
    if path.suffix == ".toml":
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise EditRefused(f"Not saved, the TOML doesn't parse: {exc}") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    log.info("web edit: %s (%d chars)", rel, len(text))
    return file_hash(path)


def effective_prefs(vault: Path) -> list[tuple[str, str, bool]]:
    """(dotted key, value, set in config.toml) for every preference in force.
    Read fresh, so it shows what the next scheduled run will use."""
    path = vault / "90-System" / "config.toml"
    try:
        yours = tomllib.loads(path.read_text("utf-8")) if path.exists() else {}
    except tomllib.TOMLDecodeError:
        yours = {}
    merged = _deep_merge(DEFAULTS, yours)
    rows: list[tuple[str, str, bool]] = []

    def walk(node: dict, mine: object, prefix: str) -> None:
        for key, value in node.items():
            here = mine.get(key) if isinstance(mine, dict) else None
            if isinstance(value, dict):
                walk(value, here, f"{prefix}{key}.")
            else:
                rows.append((prefix + key, json.dumps(value, ensure_ascii=False), here is not None))

    walk(merged, yours, "")
    return rows


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def _parse_pref(raw: str, like: object) -> object:
    """The typed value for one preference, shaped like its current one. A string
    setting takes plain text; quotes are optional."""
    raw = raw.strip()
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw
    if isinstance(like, str):
        return value if isinstance(value, str) else raw
    if isinstance(like, bool):
        if isinstance(value, bool):
            return value
        raise EditRefused("Must be true or false.")
    if isinstance(like, int) and isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(like, float) and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise EditRefused(f"Must be {'a whole number' if isinstance(like, int) else 'a number'}.")


def set_pref(vault: Path, key: str, raw: str, loaded_hash: str) -> str:
    """Set one ``table.key`` preference in config.toml, keeping the file's
    comments and layout; returns the new hash. Lists and deeper tables are
    edited in the file itself."""
    current = dict((k, v) for k, v, _ in effective_prefs(vault))
    if key not in current or key.count(".") != 1:
        raise EditRefused(f"{key} can't be set here; edit the file below.")
    like = json.loads(current[key])
    if isinstance(like, (list, dict)):
        raise EditRefused(f"{key} is a list; edit the file below.")
    value = _parse_pref(raw, like)
    table, name = key.split(".")
    path = vault / "90-System" / "config.toml"
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    line = f"{name} = {_toml_value(value)}"

    header = next((i for i, l in enumerate(lines) if re.fullmatch(rf"\s*\[{re.escape(table)}\]\s*(#.*)?", l)), None)
    if header is None:
        lines += ([""] if lines and lines[-1].strip() else []) + [f"[{table}]", line]
    else:
        end = next((i for i in range(header + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
        at = next((i for i in range(header + 1, end) if re.match(rf"\s*{re.escape(name)}\s*=", lines[i])), None)
        if at is not None:
            lines[at] = line
        else:
            last = max((i for i in range(header, end) if lines[i].strip()), default=header)
            lines.insert(last + 1, line)
    text = "\n".join(lines) + "\n"
    try:
        placed = tomllib.loads(text).get(table, {}).get(name)
    except tomllib.TOMLDecodeError:
        placed = None
    if placed != value:
        raise EditRefused(f"Couldn't place {key} in the file; edit it below.")
    return save_file(vault, "90-System/config.toml", text, loaded_hash)


def secret_status() -> list[tuple[str, bool]]:
    """(name, set?) for every key in .env.example. Never the values."""
    example = REPO_ROOT / ".env.example"
    names = re.findall(r"^([A-Z][A-Z0-9_]+)=", example.read_text("utf-8"), re.M) if example.exists() else []
    return [(name, bool(os.getenv(name))) for name in dict.fromkeys(names)]


_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _link(m: re.Match, resolve) -> str:
    text, url = m[1], m[2]
    if re.match(r"https?://", url):
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{text}</a>'
    if resolve and (note := resolve(html.unescape(url))):
        return f'<a href="#" data-note="{html.escape(note)}">{text}</a>'
    return text


def markdown_to_html(text: str, resolve=None) -> str:
    """Enough markdown for the guide and notes: headings, bullets, code blocks,
    `code`, **bold** and links. http(s) links open in a new tab; others become
    ``data-note`` links when ``resolve`` maps them to a note id, else plain text."""
    out: list[str] = []
    in_code = in_list = in_para = False
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
        line = _LINK.sub(lambda m: _link(m, resolve), line)
        text_line = line.strip() and not re.match(r"#{1,4} |- ", line.strip())
        if text_line and (in_para or in_list and raw[:1] in (" ", "\t")):
            # A hard-wrapped line continues the bullet or paragraph above it.
            end = "</li>" if out[-1].endswith("</li>") else "</p>"
            out[-1] = out[-1][: -len(end)] + " " + line.strip() + end
            continue
        in_para = False
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
            in_para = True
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
header { display:flex; gap:16px; align-items:baseline; padding:14px 20px; border-bottom:1px solid var(--line);
  position:sticky; top:0; z-index:20; background:var(--bg) }
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


# One nav for every page, the standalone architecture page included (it is
# served with its own copy swapped for this one, so the two can't drift).
NAV = ('<nav><a href="/">Dashboard</a><a href="/chat">Chat</a><a href="/brain">Brain</a>'
       '<a href="/settings">Settings</a><a href="/architecture">Architecture</a><a href="/guide">Guide</a></nav>')


def page(title: str, body: str, script: str = "", css: str = "", csrf: str = "") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{_e(title)}</title>
<meta name="qm-csrf" content="{_e(csrf)}"><style>{CSS}{css}</style></head><body><header><h1>Quartermaster</h1>
{NAV}</header><main>{body}</main>
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


CHAT_CSS = """
#chat .msg .body { margin-top:2px } #chat .msg .body > :first-child { margin-top:0 } #chat .msg .body > :last-child { margin-bottom:0 }
#chat .you .body { white-space:pre-wrap } #chat .err .body { color:var(--bad) } #chat .note .body { color:var(--muted) }
#chatstatus { min-height:1.5em; margin:8px 0 } #chatform { display:flex; gap:8px; align-items:flex-end }
#chatform textarea { flex:1; min-height:44px; max-height:240px; resize:vertical; padding:8px; font:inherit;
  border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg) }
#chatform button { font:inherit; padding:8px 16px; border-radius:6px; cursor:pointer; border:1px solid var(--accent);
  background:var(--accent); color:#fff }
"""

# The reply streams back as server-sent events over the POST's own response:
# text blocks as they're written, the current tool as a status line.
CHAT_JS = r"""
const QM_CSRF = document.querySelector('meta[name=qm-csrf]').content;
const chat = document.getElementById('chat'), form = document.getElementById('chatform');
const box = form.querySelector('textarea'), status = document.getElementById('chatstatus');
function add(who, cls, content, isHtml) {
  const d = document.createElement('div'); d.className = 'msg ' + cls;
  const w = document.createElement('span'); w.className = 'who'; w.textContent = who;
  const b = document.createElement('div'); b.className = 'body';
  if (isHtml) b.innerHTML = content; else b.textContent = content;
  d.append(w, b); chat.append(d); window.scrollTo(0, document.body.scrollHeight);
}
async function send(text) {
  add('You', 'you', text, false);
  let started = false;
  try {
    const r = await fetch('/api/chat', {method: 'POST', body: JSON.stringify({text}),
      headers: {'Content-Type': 'application/json', 'X-QM-CSRF': QM_CSRF}});
    if (!r.ok) { const d = await r.json().catch(() => ({})); add('Quartermaster', 'err', d.error || 'HTTP ' + r.status); return; }
    const reader = r.body.getReader(), dec = new TextDecoder(); let buf = '';
    for (;;) {
      const {value, done} = await reader.read(); if (done) break;
      buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!line.startsWith('data: ')) continue;
        const ev = JSON.parse(line.slice(6));
        if (ev.kind === 'start') { started = true; status.textContent = '💭 Thinking…'; }
        else if (ev.kind === 'text') { add('Quartermaster', 'qm', ev.html, true); status.textContent = '💭 Thinking…'; }
        else if (ev.kind === 'tool') status.textContent = '🔧 ' + ev.text + '…';
        else add('Quartermaster', ev.kind === 'error' ? 'err' : 'note', ev.text);
      }
    }
  } catch (e) { add('Quartermaster', 'err', 'Lost the connection: ' + e); }
  finally { if (started) status.textContent = ''; }
}
form.addEventListener('submit', e => { e.preventDefault(); const t = box.value.trim(); if (t) { box.value = ''; send(t); } });
box.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
window.scrollTo(0, document.body.scrollHeight); box.focus();
"""


def chat_page(settings: Settings, csrf: str) -> str:
    _, entries = session_entries(transcript_dir(settings.vault))
    history = "".join(
        f"<div class='msg {'you' if m['role'] == 'user' else 'qm'}'><span class='who'>"
        f"{'You' if m['role'] == 'user' else 'Quartermaster'}</span> <span class='muted'>{_e(m['at'])} via {m['source']}</span>"
        f"<div class='body'>{_e(m['text']) if m['role'] == 'user' else markdown_to_html(m['text'])}</div></div>"
        for m in entries
    )
    return page("Chat", f"""
<section><h2>Chat (the same conversation as your Discord DMs and <code>claude</code> in the vault)</h2>
<div id="chat">{history or "<p class='muted'>No conversation yet.</p>"}</div>
<div id="chatstatus" class="muted"></div>
<form id="chatform"><textarea placeholder="Message Quartermaster (Enter sends, Shift+Enter for a new line)" rows="2"></textarea>
<button type="submit">Send</button></form>
<p class="muted">"stop" cancels a running reply, "start fresh" starts a new conversation. Notion changes it proposes
are still confirmed in Discord.</p></section>""", CHAT_JS, CHAT_CSS, csrf)


EDIT_CSS = """
.file { border-top:1px solid var(--line); padding:10px 0 }
.file:first-of-type { border-top:0 }
.filehead { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap }
.filehead button, .editor button { font:inherit; font-size:12px; padding:3px 10px; border-radius:5px; cursor:pointer;
  border:1px solid var(--line); background:var(--code); color:var(--fg) }
.editor button[data-save] { background:var(--accent); border-color:var(--accent); color:#fff }
.editor textarea { width:100%; min-height:260px; margin:6px 0; padding:8px; border:1px solid var(--line); border-radius:6px;
  background:var(--bg); color:var(--fg); font:12.5px/1.5 ui-monospace, Consolas, monospace; resize:vertical }
.view { overflow-wrap:anywhere } .view h2 { font-size:14px; text-transform:none; letter-spacing:0; color:var(--fg) }
.msg { font-size:12px } .note { color:var(--muted); font-size:12.5px; margin:0 0 8px }
td.v { font:12px ui-monospace, Consolas, monospace; overflow-wrap:anywhere }
"""

# Shared by /settings and the brain's note panel: any .file block becomes
# editable in place. The CSRF header is what a cross-site form can't send.
EDIT_JS = r"""
const QM_CSRF = document.querySelector('meta[name=qm-csrf]').content;
function qmClose(box) {
  box.querySelector('.editor').hidden = true; box.querySelector('.view').hidden = false;
  box.querySelector('[data-edit]').hidden = false;
}
async function qmSave(box) {
  const ta = box.querySelector('textarea'), msg = box.querySelector('.msg');
  msg.className = 'msg muted'; msg.textContent = 'Saving…';
  let r = null, d = {};
  try {
    r = await fetch('/api/file', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-QM-CSRF': QM_CSRF},
      body: JSON.stringify({path: box.dataset.path, text: ta.value, hash: box.dataset.hash})});
    d = await r.json();
  } catch (e) { d = {error: 'Save failed: ' + e}; }
  if (!r || !r.ok) { msg.className = 'msg bad'; msg.textContent = d.error || 'Save failed.'; return; }
  box.dataset.hash = d.hash; ta.dataset.orig = ta.value; box.querySelector('.view').innerHTML = d.html;
  qmClose(box); msg.className = 'msg ok'; msg.textContent = 'Saved.';
  setTimeout(() => { if (msg.textContent === 'Saved.') msg.textContent = ''; }, 2500);
}
document.addEventListener('click', ev => {
  const b = ev.target.closest('[data-edit],[data-save],[data-cancel]');
  if (!b) return;
  const box = b.closest('.file'), ta = box.querySelector('textarea');
  if (b.hasAttribute('data-edit')) {
    box.querySelector('.editor').hidden = false; box.querySelector('.view').hidden = true; b.hidden = true;
    box.querySelector('.msg').textContent = ''; ta.dataset.orig = ta.value; ta.focus();
  } else if (b.hasAttribute('data-cancel')) { ta.value = ta.dataset.orig; qmClose(box); }
  else qmSave(box);
});
document.addEventListener('keydown', ev => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key === 's' && ev.target.matches('.file textarea')) {
    ev.preventDefault(); qmSave(ev.target.closest('.file'));
  }
});
"""


PREF_CSS = """
#prefs td.v[data-pref] { cursor:text; border-radius:4px }
#prefs td.v[data-pref]:hover { background:var(--code); box-shadow:inset 0 0 0 1px var(--line) }
#prefs input { width:100%; box-sizing:border-box; padding:3px 6px; border:1px solid var(--accent); border-radius:5px;
  background:var(--bg); color:var(--fg); font:12px ui-monospace, Consolas, monospace }
#prefs .msg { display:block; margin-top:3px; font-family:inherit }
"""

# Click a value to change it: Enter saves into config.toml (comments kept), Esc
# or clicking away cancels. The page reloads after a save so the table, the file
# and its hash agree.
PREF_JS = r"""
document.addEventListener('click', ev => {
  const cell = ev.target.closest('td[data-pref]');
  if (!cell || cell.querySelector('input')) return;
  const orig = cell.textContent, input = document.createElement('input'), msg = document.createElement('span');
  input.value = orig.startsWith('"') ? JSON.parse(orig) : orig;
  msg.className = 'msg';
  cell.textContent = ''; cell.append(input, msg); input.focus(); input.select();
  let saving = false;
  const done = () => { if (!saving) cell.textContent = orig; };
  input.addEventListener('blur', done);
  input.addEventListener('keydown', async e => {
    if (e.key === 'Escape') return done();
    if (e.key !== 'Enter' || saving) return;
    saving = true; msg.className = 'msg muted'; msg.textContent = 'Saving…';
    let r = null, d = {};
    try {
      r = await fetch('/api/pref', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-QM-CSRF': QM_CSRF},
        body: JSON.stringify({key: cell.closest('tr').dataset.key, value: input.value,
                              hash: document.getElementById('prefs').dataset.hash})});
      d = await r.json();
    } catch (err) { d = {error: 'Save failed: ' + err}; }
    if (r && r.ok) return location.reload();
    saving = false; msg.className = 'msg bad'; msg.textContent = d.error || 'Save failed.'; input.focus();
  });
});
"""


def render_file(rel: str, text: str) -> str:
    """How an editable file reads when it isn't being edited. The title is in the
    block's header, so a leading ``# Heading`` isn't repeated; TOML sits behind a
    toggle because the preferences table above already shows what's in force."""
    if rel.endswith(".toml"):
        return f"<details><summary class='muted'>Show the file</summary><pre>{_e(text)}</pre></details>"
    return markdown_to_html(re.sub(r"\A\s*# .*\n?", "", text)) or "<p class='muted'>Empty.</p>"


def file_block(vault: Path, rel: str, title: str, note: str = "") -> str:
    path = editable_path(vault, rel)
    text = path.read_text("utf-8") if path and path.exists() else ""
    view = render_file(rel, text) if text.strip() else "<p class='muted'>Nothing here yet.</p>"
    return (
        f"<div class='file' data-path='{_e(rel)}' data-hash='{file_hash(path) if path else ''}'>"
        f"<div class='filehead'><strong>{_e(title)}</strong> <span class='muted'>{_e(rel)}</span>"
        f"<button type='button' data-edit>Edit</button><span class='msg'></span></div>"
        + (f"<p class='note'>{note}</p>" if note else "")
        + f"<div class='view'>{view}</div>"
        f"<div class='editor' hidden><textarea spellcheck='false'>{_e(text)}</textarea>"
        f"<button type='button' data-save>Save</button> <button type='button' data-cancel>Cancel</button>"
        f" <span class='muted'>Ctrl+S saves</span></div></div>"
    )


FACT_NOTES = {
    "facts/lessons.md": "Corrections you've given. The agent records one whenever you tell it it got something "
                        "wrong, and every reply and digest follows them.",
    "facts/interests.md": "Filters the digest's events and the presale pings. A line you write outranks anything inferred.",
}


def _fact_title(path: Path) -> str:
    if not path.exists():
        return "Lessons"
    m = re.search(r"^# (.+)$", path.read_text("utf-8"), re.M)
    return m[1].strip() if m else path.stem


def settings_page(settings: Settings, csrf: str) -> str:
    vault = settings.vault
    prefs = "".join(
        f"<tr data-key='{_e(key)}'><td><code>{_e(key)}</code></td><td class='v'"
        + ("" if value.startswith(("[", "{")) or key.count(".") != 1 else " data-pref title='Click to change'")
        + f">{_e(value)}</td></tr>"
        for key, value, _ in effective_prefs(vault)
    )
    config_hash = file_hash(vault / "90-System" / "config.toml")
    env = "".join(
        f"<tr><td><code>{_e(name)}</code></td><td class='{'ok' if is_set else 'muted'}'>{'set' if is_set else 'not set'}</td></tr>"
        for name, is_set in secret_status()
    )
    facts = {p.relative_to(vault).as_posix() for p in settings.facts_dir.glob("*.md")} if settings.facts_dir.exists() else set()
    facts.add("facts/lessons.md")  # shown even before the first lesson, so it can be seeded by hand
    order = sorted(facts, key=lambda rel: (rel != "facts/lessons.md", rel.endswith("/README.md"), rel))
    fact_blocks = "".join(file_block(vault, rel, _fact_title(vault / rel), FACT_NOTES.get(rel, "")) for rel in order)
    return page("Settings", f"""
<section><h2>How changes apply</h2><p class="note" style="margin:0">Everything Quartermaster runs on is on this page.
Instructions, facts and lessons apply from the next message. Preferences apply to scheduled jobs on their next run
and to the bot after a restart, except <code>chat.*</code>, which applies from the next message. Click a value to change
it (Enter saves, Esc cancels). A save is refused if the agent changed the file since you opened it. Notion pages
are edited in Notion: the mirror is overwritten on every sync.</p></section>
<section><h2>Preferences in force</h2><table id="prefs" data-hash="{config_hash}"><tr><th>Setting</th><th>Value</th></tr>{prefs}</table>
{file_block(vault, "90-System/config.toml", "Edit preferences", "The whole file, for lists like the distance bands. Saved only if it parses.")}</section>
<section><h2>Conflicts</h2>{file_block(vault, "90-System/conflicts.md", "Where what it knows disagrees", "Found by the daily reconcile (<code>qm reconcile</code>). Answer in a DM and every file gets updated, or fix it yourself and delete the entry.")}</section>
<section><h2>What it knows</h2>{fact_blocks}</section>
<section><h2>Instructions</h2>{file_block(vault, "CLAUDE.md", "How the agent works in your vault", "Loaded at the start of every conversation and into every digest.")}</section>
<div class="grid2">
<section><h2>Mutes</h2>{file_block(vault, "90-System/muted.md", "Never raise these again", "One <code>kind:key</code> per line. <code>artist/Tool</code> with no kind mutes every kind.")}</section>
<section><h2>Dev queue</h2>{file_block(vault, "90-System/dev-queue.md", "Changes to Quartermaster itself", "Worked in Claude Code. Tick an item with <code>[x]</code> to close it.")}</section>
</div>
<section><h2>Secrets</h2><p class="note">In the repo's <code>.env</code>. Shown as set or not, never their values, and not editable from a browser.</p>
<table>{env}</table></section>""", EDIT_JS + PREF_JS, EDIT_CSS + PREF_CSS, csrf)


# --- App -----------------------------------------------------------------------


LOCAL_HOSTS = ("127.0.0.1", "localhost")


def build_app(settings: Settings, token: str | None = None, hosts: tuple[str, ...] = LOCAL_HOSTS):
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
    from starlette.routing import Route

    # Per run: pages carry it, saves must send it back as a header.
    csrf = secrets.token_urlsafe(32)

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

    async def architecture(request: Request):
        # A standalone page in docs/, so it also reads fine opened from the repo.
        path = REPO_ROOT / "docs" / "architecture.html"
        if not path.exists():
            return PlainTextResponse("docs/architecture.html is missing.", status_code=404)
        return HTMLResponse(re.sub(r"<nav>.*?</nav>", lambda _: NAV, path.read_text("utf-8"), count=1, flags=re.S))

    def graph() -> dict:
        _, log_text = read_log_from(settings.log_path, -TAIL_BYTES)
        return brain.build(settings.vault, log_text)

    async def brain_page(request: Request):
        return HTMLResponse(page("Brain", brain.BODY, EDIT_JS + brain.JS, EDIT_CSS + brain.CSS, csrf))

    async def settings_view(request: Request):
        return HTMLResponse(settings_page(settings, csrf))

    async def api_file_save(request: Request):
        if not secrets.compare_digest(request.headers.get("x-qm-csrf", ""), csrf):
            return JSONResponse({"error": "Missing or stale page token. Reload the page."}, status_code=403)
        try:
            body = await request.json()
            rel, text = str(body["path"]), str(body["text"])
            new_hash = save_file(settings.vault, rel, text, str(body.get("hash", "")))
        except EditRefused as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "Malformed save."}, status_code=400)
        saved = (settings.vault / rel).read_text("utf-8")
        return JSONResponse({"hash": new_hash, "html": render_file(rel, saved)})

    async def api_pref_save(request: Request):
        if not secrets.compare_digest(request.headers.get("x-qm-csrf", ""), csrf):
            return JSONResponse({"error": "Missing or stale page token. Reload the page."}, status_code=403)
        try:
            body = await request.json()
            new_hash = set_pref(settings.vault, str(body["key"]), str(body["value"]), str(body.get("hash", "")))
        except EditRefused as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "Malformed save."}, status_code=400)
        return JSONResponse({"hash": new_hash})

    async def chat_view(request: Request):
        return HTMLResponse(chat_page(settings, csrf))

    # The web chat's own turn, as the bot keeps its own: "stop" here cancels
    # this one. chat.TurnLock keeps it from overlapping a Discord turn.
    owner = agent.owner_profile(settings)
    turn: dict = {"task": None, "fresh": False}

    def events(queue: asyncio.Queue):
        async def stream():
            while (event := await queue.get()) is not None:
                yield f"data: {json.dumps(event)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    def note(text: str):
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"kind": "note", "text": text})
        queue.put_nowait(None)
        return events(queue)

    async def api_chat(request: Request):
        if not secrets.compare_digest(request.headers.get("x-qm-csrf", ""), csrf):
            return JSONResponse({"error": "Missing or stale page token. Reload the page."}, status_code=403)
        try:
            text = str((await request.json())["text"]).strip()
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "Malformed message."}, status_code=400)
        if not text:
            return JSONResponse({"error": "Empty message."}, status_code=400)

        running = turn["task"] is not None and not turn["task"].done()
        control = chat.session_control(text)
        if control == "stop":
            if running:
                turn["task"].cancel()
                return note("Stopping.")
            return note("Nothing is running here.")
        if control == "new":
            turn["fresh"] = True
            return note(chat.FRESH_NOTE)
        if running:
            return note('Still working on the last one. Say "stop" to cancel it.')
        lock = chat.TurnLock(chat.lock_path(settings))
        if not lock.acquire():
            return note("Busy with a message from Discord. Try again when it's answered.")

        profile = chat.continue_or_fresh(settings, owner)
        if turn["fresh"]:
            profile, turn["fresh"] = replace(owner, share_session=False), False
        queue: asyncio.Queue = asyncio.Queue()
        sent = False

        async def progress(kind: str, payload: object) -> None:
            nonlocal sent
            if kind == "text" and str(payload).strip():
                queue.put_nowait({"kind": "text", "html": markdown_to_html(str(payload).strip())})
                sent = True
            elif kind == "tool":
                name, tool_input = payload  # type: ignore[misc]
                queue.put_nowait({"kind": "tool", "text": chat.describe_tool(name, tool_input or {})})

        async def run() -> None:
            # A task of its own, not the response's: closing the tab mid-turn
            # leaves the turn to finish, and its reply lands in the transcript.
            try:
                reply = await agent.ask(text, profile, settings.claude_cli, on_progress=progress)
                if reply.error:
                    queue.put_nowait({"kind": "error", "text": reply.error})
                elif not sent:
                    queue.put_nowait({"kind": "error", "text": "I finished but produced no reply. That's a bug."})
            except asyncio.CancelledError:
                log.info("web chat turn stopped by the owner")
                queue.put_nowait({"kind": "note", "text": "⏹️ Stopped."})
            finally:
                lock.release()
                queue.put_nowait(None)

        queue.put_nowait({"kind": "start"})
        turn["task"] = asyncio.create_task(run())
        return events(queue)

    async def api_brain(request: Request):
        return JSONResponse(graph())

    async def api_brain_note(request: Request):
        note_id = request.query_params.get("id", "")
        path = brain.note_path(settings.vault, note_id)
        if path is None:
            return JSONResponse({"error": "No such note."}, status_code=404)
        meta, body = brain.frontmatter(path.read_text("utf-8", errors="replace"))
        root = settings.vault.resolve()

        def resolve(target: str) -> str | None:
            candidate = (path.parent / target.split("#")[0]).resolve()
            if root not in candidate.parents:
                return None
            rel = candidate.relative_to(root).as_posix()
            return rel if brain.note_path(settings.vault, rel) else None

        g = graph()
        node = next((n for n in g["nodes"] if n["id"] == note_id), {})
        out, back = brain.neighbours(g, note_id)
        url = meta.get("url", "")
        editable = editable_path(settings.vault, note_id) is not None
        body = re.sub(r"\A\s*# .*\n?", "", body)  # the panel header already shows the title
        return JSONResponse({**node, "html": markdown_to_html(body, resolve), "out": out, "back": back,
                             "url": url if url.startswith("https://") else "", "editable": editable,
                             **({"raw": path.read_text("utf-8"), "hash": file_hash(path)} if editable else {})})

    return Starlette(middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=list(hosts))], routes=[
        Route("/", guarded(index)),
        Route("/chat", guarded(chat_view)),
        Route("/api/chat", guarded(api_chat), methods=["POST"]),
        Route("/settings", guarded(settings_view)),
        Route("/architecture", guarded(architecture)),
        Route("/api/file", guarded(api_file_save), methods=["POST"]),
        Route("/api/pref", guarded(api_pref_save), methods=["POST"]),
        Route("/brain", guarded(brain_page)),
        Route("/api/brain", guarded(api_brain)),
        Route("/api/brain/note", guarded(api_brain_note)),
        Route("/api/log", guarded(api_log)),
        Route("/digest/{name}", guarded(digest)),
        Route("/guide", guarded(guide)),
    ])


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8766) -> int:
    import uvicorn

    token = os.getenv("QM_WEB_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise RuntimeError(f"Refusing to serve on {host} without QM_WEB_TOKEN set: the log holds DMs and email snippets.")
    hosts = LOCAL_HOSTS if host in LOCAL_HOSTS else (*LOCAL_HOSTS, host)
    uvicorn.run(build_app(settings, token, hosts), host=host, port=port, log_level="warning")
    return 0
