"""What every page shares: the stylesheet, the nav, the page shell, escaping,
and the small markdown renderer used for the guide and the editors.
"""

from __future__ import annotations

import html
import re
from datetime import datetime


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
td.nw, th { white-space:nowrap } td.grow { width:100%; overflow-wrap:anywhere }
time[title] { cursor:help }
details.clip > summary { cursor:pointer; list-style:none }
details.clip > summary::-webkit-details-marker { display:none }
details.clip > summary::after { content:"more"; color:var(--accent); font-size:12px; margin-left:6px }
details.clip[open] > summary .short { display:none }
details.clip[open] > summary::after { content:"less"; margin-left:0 }
details.clip .full { white-space:pre-wrap; overflow-wrap:anywhere }
"""

# Sections with an id and ``data-live`` are re-fetched from the same URL every
# LIVE_SECONDS and swapped in when the server's copy changed, so a page left
# open stays current. A section being used is left alone: focus or a text
# selection inside it, or an open editor. Open "more" toggles stay open.
# Paused while the tab is hidden or ``window.qmBusy`` is set (a chat reply
# streaming in); refreshes at once when the tab comes back.
LIVE_SECONDS = 15
LIVE_JS = """
(() => {
  const served = new WeakMap(), live = () => [...document.querySelectorAll('[data-live][id]')];
  if (!live().length) return;
  live().forEach(el => served.set(el, el.outerHTML));
  const inUse = el => (el.contains(document.activeElement) && document.activeElement !== document.body)
    || (getSelection().toString() && el.contains(getSelection().anchorNode))
    || [...el.querySelectorAll('.editor')].some(e => !e.hidden);
  async function refresh() {
    if (document.hidden || window.qmBusy) return;
    let doc;
    try {
      const r = await fetch(location.href, {cache: 'no-store'});
      if (!r.ok) return;
      doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    } catch (e) { return; }
    const stick = innerHeight + scrollY >= document.body.scrollHeight - 40;
    for (const el of live()) {
      const fresh = doc.getElementById(el.id);
      if (!fresh || fresh.outerHTML === served.get(el) || inUse(el)) continue;
      const open = new Set([...el.querySelectorAll('details[open] > summary')].map(s => s.textContent));
      for (const a of [...el.attributes]) if (!fresh.hasAttribute(a.name)) el.removeAttribute(a.name);
      for (const a of fresh.attributes) el.setAttribute(a.name, a.value);
      el.innerHTML = fresh.innerHTML;  // the same element: scripts holding it keep working
      el.querySelectorAll('details > summary').forEach(s => { if (open.has(s.textContent)) s.parentElement.open = true; });
      served.set(el, fresh.outerHTML);
      if (stick && el.hasAttribute('data-live-stick')) scrollTo(0, document.body.scrollHeight);
    }
  }
  setInterval(refresh, LIVE_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();
""".replace("LIVE_MS", str(LIVE_SECONDS * 1000))

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


def clip(text: object, limit: int = 120, full_html: str | None = None) -> str:
    """Text that may be long: up to ``limit`` characters of its first line, with
    a "more" toggle that shows all of it. ``full_html`` is what the toggle
    shows instead of the escaped text (rendered markdown, a <pre>)."""
    text = str(text)
    if len(text) <= limit and "\n" not in text.strip():
        return full_html if full_html is not None else _e(text)
    short = text.strip().split("\n", 1)[0][:limit].rstrip()
    full = full_html if full_html is not None else f"<div class='full'>{_e(text)}</div>"
    return f"<details class='clip'><summary><span class='short'>{_e(short)}…</span></summary>{full}</details>"


_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %I:%M:%S %p")


def short_time(text: str, now: datetime | None = None) -> str:
    """"today 7:00 am", "tomorrow 3:00 am", "Sep 27 9:00 am" from a log or
    Task Scheduler timestamp; anything else ("never", "N/A") as it is."""
    for fmt in _TIME_FORMATS:
        try:
            when = datetime.strptime(text.strip(), fmt)
            break
        except ValueError:
            continue
    else:
        return text
    days = (when.date() - (now or datetime.now()).date()).days
    day = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days, f"{when:%b} {when.day}")
    return f"{day} {when.hour % 12 or 12}:{when.minute:02d} {'am' if when.hour < 12 else 'pm'}"


def time_html(text: str) -> str:
    """``short_time`` with the full timestamp on hover."""
    short = short_time(text)
    return _e(short) if short == text else f"<time title='{_e(text)}'>{_e(short)}</time>"


# One nav for every page, the standalone architecture page included (it is
# served with its own copy swapped for this one, so the two can't drift).
NAV = ('<nav><a href="/">Dashboard</a><a href="/chat">Chat</a><a href="/brain">Brain</a><a href="/servers">Servers</a>'
       '<a href="/settings">Settings</a><a href="/architecture">Architecture</a><a href="/guide">Guide</a></nav>')


def page(title: str, body: str, script: str = "", css: str = "", csrf: str = "") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{_e(title)}</title>
<meta name="qm-csrf" content="{_e(csrf)}"><style>{CSS}{css}</style></head><body><header><h1>Quartermaster</h1>
{NAV}</header><main>{body}</main>
<script>{script}</script><script>{LIVE_JS}</script></body></html>"""
