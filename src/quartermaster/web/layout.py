"""What every page shares: the stylesheet, the nav, the page shell, escaping,
and the small markdown renderer used for the guide and the editors.
"""

from __future__ import annotations

import html
import re


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
NAV = ('<nav><a href="/">Dashboard</a><a href="/chat">Chat</a><a href="/brain">Brain</a><a href="/servers">Servers</a>'
       '<a href="/settings">Settings</a><a href="/architecture">Architecture</a><a href="/guide">Guide</a></nav>')


def page(title: str, body: str, script: str = "", css: str = "", csrf: str = "") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{_e(title)}</title>
<meta name="qm-csrf" content="{_e(csrf)}"><style>{CSS}{css}</style></head><body><header><h1>Quartermaster</h1>
{NAV}</header><main>{body}</main>
<script>{script}</script></body></html>"""
