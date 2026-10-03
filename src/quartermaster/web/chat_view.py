"""``/chat``: the owner's conversation, the same session as the Discord DMs.
"""

from __future__ import annotations

from ..agent import transcript_dir
from ..config import Settings
from .dashboard_view import session_entries
from .layout import _e, markdown_to_html, page


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
