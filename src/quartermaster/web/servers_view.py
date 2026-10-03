"""``/servers``: a tab per game server with its status, start/stop, last
backup and live console.
"""

from __future__ import annotations

from pathlib import Path

from .layout import _e, page


SERVERS_CSS = """
.tabs { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:12px }
.tabs a { padding:4px 10px; border:1px solid var(--line); border-radius:6px; text-decoration:none; color:var(--fg) }
.tabs a.on { background:var(--code); font-weight:600 }
#serverlog { height:520px } .actions { display:flex; gap:8px; align-items:center; margin:8px 0 12px }
"""

SERVERS_JS = """
const QM_CSRF = document.querySelector('meta[name=qm-csrf]').content;
const game = document.getElementById('server').dataset.game;
const el = document.getElementById('serverlog'), st = document.getElementById('serverstatus'),
      msg = document.getElementById('servermsg');
let pos = -20000;
async function pollLog() {
  try {
    const d = await (await fetch(`/api/servers/${game}/log?pos=${pos}`)).json();
    if (d.reset) el.textContent = '';
    if (d.text) { const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
      el.textContent += d.text; if (el.textContent.length > 400000) el.textContent = el.textContent.slice(-300000);
      if (stick) el.scrollTop = el.scrollHeight; }
    pos = d.pos;
  } catch (e) {}
  setTimeout(pollLog, 2000);
}
async function pollStatus() {
  try { st.textContent = (await (await fetch(`/api/servers/${game}/status`)).json()).status; } catch (e) {}
  setTimeout(pollStatus, 10000);
}
document.querySelectorAll('[data-action]').forEach(b => b.addEventListener('click', async () => {
  const action = b.dataset.action;
  if (action === 'stop' && !confirm('Stop the server? Anyone on it is disconnected.')) return;
  document.querySelectorAll('[data-action]').forEach(x => x.disabled = true);
  msg.className = 'muted'; msg.textContent = action === 'stop' ? 'Stopping (saving the world)…' : 'Starting…';
  try {
    const r = await fetch(`/api/servers/${game}/${action}`, {method: 'POST', headers: {'X-QM-CSRF': QM_CSRF}});
    const d = await r.json(); msg.className = r.ok ? 'ok' : 'bad'; msg.textContent = d.result || d.error;
  } catch (e) { msg.className = 'bad'; msg.textContent = 'Request failed.'; }
  document.querySelectorAll('[data-action]').forEach(x => x.disabled = false);
  st.textContent = (await (await fetch(`/api/servers/${game}/status`)).json()).status;
}));
pollLog(); pollStatus();
"""


def servers_page(key: str, status: str, log_path: Path, csrf: str, backup: str = "") -> str:
    """One tab per game; the chosen one's status, start/stop and live console."""
    from ..games import GAMES

    game = GAMES[key]
    tabs = "".join(f"<a href='/servers/{g.key}' class='{'on' if g.key == key else ''}'>{_e(g.title)}</a>"
                   for g in GAMES.values())
    return page("Servers", f"""
<section id="server" data-game="{_e(key)}"><h2>Game servers</h2><div class="tabs">{tabs}</div>
<p><strong>{_e(game.title)}</strong>: <span id="serverstatus">{_e(status)}</span></p>
<div class="actions"><button type="button" data-action="start">Start</button>
<button type="button" data-action="stop">Stop</button><span id="servermsg"></span></div>
<pre id="serverlog"></pre>
<p class="muted">The console of the current (or last) run: <code>{_e(log_path)}</code>.
Commands go through the bot in Discord.</p>
{f'<p class="muted">{_e(backup)}</p>' if backup else ''}
</section>""", SERVERS_JS, SERVERS_CSS, csrf)
