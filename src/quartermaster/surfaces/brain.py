"""The vault as a graph, for ``qm web``'s /brain page.

Nodes are the vault's knowledge: ``KNOWLEDGE_DIRS``, the Notion mirror (what
the owner wrote) and ``facts/`` (what the agent has distilled and remembers).
Digests, the inbox, system files and folder READMEs are working material, not
memory, so they're left out; an inbox item appears once it's filed into facts.
Edges come from what's already there, nothing is inferred by a model:

- ``link``: a relative markdown link or ``[[wikilink]]`` to another note.
- ``folder``: a mirrored page to its Notion parent (``gaming/x.md`` sits under
  ``gaming-<id>.md``), when no link already joins them.
- ``mention``: a note naming another note's title as a whole word. Titles found
  in more than ``MENTION_CAP`` of the vault ("Notes", "Files") say nothing and
  are skipped.

Each node also carries how recently it changed (Notion's ``last_edited`` for
the mirror, since every sync rewrites the file) and whether the agent read or
wrote it in the recent log, which is what makes it look alive.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

KNOWLEDGE_DIRS = ("facts", "notion")
MENTION_CAP = 0.15  # a title named in more than this share of notes is noise
MIN_TITLE = 4

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
_MD_LINK = re.compile(r"\]\(([^)\s]+)\)")
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
_NOTION_ID = re.compile(r"-[0-9a-f]{8}$")
_TOOL_PATH = re.compile(r"\[\w+\] tool call: (Read|Write|Edit)\(\{.*?'file_path': '((?:[^'\\]|\\.)*)'")


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    """(simple key: value pairs, body). Enough for the mirror's flat frontmatter."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m[1].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip('"')
    return meta, text[m.end():]


def is_knowledge(rel: Path) -> bool:
    """Whether a vault-relative path is a note the graph shows."""
    return (len(rel.parts) > 1 and rel.parts[0] in KNOWLEDGE_DIRS and rel.suffix == ".md"
            and rel.name.lower() != "readme.md" and not any(p.startswith(".") for p in rel.parts))


def note_path(vault: Path, note_id: str) -> Path | None:
    """The file for a node id, or None if it isn't a note the graph shows."""
    path = (vault / note_id).resolve()
    root = vault.resolve()
    if root not in path.parents or not path.is_file() or not is_knowledge(path.relative_to(root)):
        return None
    return path


def _title(meta: dict, body: str, path: Path) -> str:
    if meta.get("title"):
        return meta["title"]
    if m := re.search(r"^# (.+)$", body, re.M):
        return m[1].strip()
    return _NOTION_ID.sub("", path.stem).replace("-", " ")


def _group(rel: Path) -> str:
    """The top folder, except the Notion mirror (most of the vault) is split by
    its top-level page: notion/lifestyle-<id>.md and notion/lifestyle/* share one."""
    if rel.parts[0] != "notion":
        return rel.parts[0]
    if len(rel.parts) > 2:
        return rel.parts[1]
    return _NOTION_ID.sub("", rel.stem) if _NOTION_ID.search(rel.stem) else "notion"


def _edited(meta: dict, path: Path) -> float:
    try:
        return datetime.fromisoformat(meta["last_edited"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return path.stat().st_mtime


def touched_by_agent(log_text: str, vault: Path) -> dict[str, int]:
    """Note id -> how many times the agent read or wrote it in ``log_text``."""
    root = vault.resolve()
    counts: dict[str, int] = {}
    for m in _TOOL_PATH.finditer(log_text):
        raw = m[2].replace("\\\\", "\\")
        path = Path(raw) if Path(raw).is_absolute() else root / raw
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        counts[rel] = counts.get(rel, 0) + 1
    return counts


def build(vault: Path, log_text: str = "", now: float | None = None) -> dict:
    """{"nodes": [...], "edges": [...]} for the vault's knowledge."""
    now = now or time.time()
    root = vault.resolve()
    notes: dict[str, dict] = {}
    bodies: dict[str, str] = {}
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if not is_knowledge(rel):
            continue
        meta, body = frontmatter(path.read_text("utf-8", errors="replace"))
        note_id = rel.as_posix()
        notes[note_id] = {
            "id": note_id,
            "title": _title(meta, body, path),
            "group": _group(rel),
            "age_days": round(max(0.0, now - _edited(meta, path)) / 86400, 1),
            "words": len(body.split()),
        }
        bodies[note_id] = body
    sizes: dict[str, int] = {}
    for note in notes.values():
        sizes[note["group"]] = sizes.get(note["group"], 0) + 1
    for note_id, note in notes.items():
        if sizes[note["group"]] == 1 and note_id.startswith("notion/"):
            note["group"] = "notion"  # a one-page section is noise in the legend

    edges: dict[frozenset, dict] = {}

    def add(a: str, b: str, kind: str) -> None:
        if a == b or a not in notes or b not in notes:
            return
        key = frozenset((a, b))
        rank = {"link": 0, "folder": 1, "mention": 2}
        if key not in edges or rank[kind] < rank[edges[key]["kind"]]:
            edges[key] = {"source": a, "target": b, "kind": kind}

    by_name = {}
    for note_id, note in notes.items():
        by_name.setdefault(note["title"].lower(), note_id)
        by_name.setdefault(Path(note_id).stem.lower(), note_id)

    for note_id, body in bodies.items():
        here = (root / note_id).parent
        for target in _MD_LINK.findall(body):
            if re.match(r"[a-z]+:", target, re.I) or target.startswith("#"):
                continue
            target = target.split("#")[0]
            try:
                rel = (here / target).resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            add(note_id, rel, "link")
        for name in _WIKILINK.findall(body):
            if target := by_name.get(name.strip().lower()):
                add(note_id, target, "link")

    for note_id in notes:
        folder = Path(note_id).parent
        parent = next((other for other in sorted(notes) if Path(other).parent == folder.parent
                       and _NOTION_ID.sub("", Path(other).stem) == folder.name), None)
        if parent:
            add(parent, note_id, "folder")

    titles = {
        note_id: note["title"] for note_id, note in notes.items()
        if len(note["title"]) >= MIN_TITLE and re.search(r"[^\W\d_]", note["title"])
    }
    patterns = {note_id: re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)", re.I) for note_id, t in titles.items()}
    cap = max(3, int(len(notes) * MENTION_CAP))
    for target, pattern in patterns.items():
        hits = [note_id for note_id, body in bodies.items() if note_id != target and pattern.search(body)]
        if len(hits) <= cap:
            for source in hits:
                add(source, target, "mention")

    degree: dict[str, int] = {}
    for edge in edges.values():
        degree[edge["source"]] = degree.get(edge["source"], 0) + 1
        degree[edge["target"]] = degree.get(edge["target"], 0) + 1
    touched = touched_by_agent(log_text, root)
    for note_id, note in notes.items():
        note["degree"] = degree.get(note_id, 0)
        note["touched"] = touched.get(note_id, 0)
    return {"nodes": list(notes.values()), "edges": list(edges.values())}


def neighbours(graph: dict, note_id: str) -> tuple[list[dict], list[dict]]:
    """(notes this one points at, notes pointing at it), as {id, title, kind}."""
    titles = {n["id"]: n["title"] for n in graph["nodes"]}
    out, back = [], []
    for e in graph["edges"]:
        if e["source"] == note_id:
            out.append({"id": e["target"], "title": titles[e["target"]], "kind": e["kind"]})
        elif e["target"] == note_id:
            back.append({"id": e["source"], "title": titles[e["source"]], "kind": e["kind"]})
    return out, back


# --- Page ----------------------------------------------------------------------
# Canvas and a small force layout, no libraries: the dashboard has to work
# offline and over Tailscale, and ~100 notes don't need d3.

BODY = """
<section id="brainwrap">
  <canvas id="brain" aria-label="The vault as a graph of linked notes"></canvas>
  <div id="hud">
    <input id="q" type="search" placeholder="Find a note" autocomplete="off">
    <div id="stats" class="muted"></div>
    <div id="legend"></div>
  </div>
  <div id="tip" hidden></div>
  <aside id="panel" hidden><button id="close" aria-label="Close">&times;</button><div id="note"></div></aside>
</section>"""

CSS = """
main { max-width:none; padding:12px }
#brainwrap { position:relative; padding:0; height:calc(100vh - 90px); min-height:420px; overflow:hidden;
  background:radial-gradient(ellipse at center, #141a2a 0%, #0a0c12 70%); border-color:#1c2130 }
#brain { display:block; width:100%; height:100%; cursor:grab; touch-action:none }
#hud { position:absolute; top:12px; left:12px; display:grid; gap:6px; max-width:260px; color:#c9cfdc; font-size:12px }
#hud .muted { color:#8a93a8 }
#q { background:#151a26; color:#e6e9f0; border:1px solid #2a3144; border-radius:6px; padding:6px 8px; font:inherit; font-size:13px }
#legend span { display:inline-flex; align-items:center; gap:4px; margin:0 10px 2px 0; white-space:nowrap }
#legend i { width:9px; height:9px; border-radius:50%; display:inline-block }
#legend b { font-weight:500; color:#8a93a8 }
#tip { position:absolute; pointer-events:none; background:#151a26ee; color:#e6e9f0; border:1px solid #2a3144;
  border-radius:6px; padding:5px 8px; font-size:12px; max-width:320px }
#tip .muted { color:#8a93a8 }
#panel { position:absolute; top:0; right:0; bottom:0; width:min(420px, 100%); overflow:auto; background:var(--card);
  border-left:1px solid var(--line); padding:14px 16px }
#panel h3 { margin:0 24px 4px 0; font-size:17px } #panel .meta { color:var(--muted); font-size:12px; margin-bottom:10px }
#panel ul.rel { padding-left:18px; margin:4px 0 10px } #panel .kind { color:var(--muted); font-size:11px }
#panel .body { border-top:1px solid var(--line); margin-top:10px; padding-top:6px; overflow-wrap:anywhere }
#panel .editor textarea { min-height:50vh }
#close { position:absolute; top:8px; right:10px; background:none; border:0; color:var(--muted); font-size:22px; cursor:pointer }
@media (max-width: 800px) { #hud { max-width:calc(100% - 24px) } #legend { display:none } }
"""

JS = r"""
const C = document.getElementById('brain'), X = C.getContext('2d');
const tip = document.getElementById('tip'), panel = document.getElementById('panel'), noteEl = document.getElementById('note');
const PALETTE = ['#7aa2ff', '#5fd4a8', '#f5b86b', '#e98bd8', '#9ad06a', '#6ad3e8', '#f08a80', '#c3a6ff', '#e3d26f', '#ff9f5a', '#8fb8c9', '#d8a6a6'];
const EDGE = {link: ['#8aa4ff', .45, 60], folder: ['#7c8599', .3, 55], mention: ['#c792ea', .22, 120]};
let W = 0, H = 0, dpr = 1, view = {x: 0, y: 0, k: 1}, alpha = 1;
let nodes = [], edges = [], byId = new Map(), adj = new Map(), color = {}, matches = null;
let hover = null, selected = null, drag = null;

const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const ago = d => d < 1 ? 'today' : d < 2 ? 'yesterday' : d < 60 ? Math.round(d) + ' days ago' : Math.round(d / 30) + ' months ago';

fetch('/api/brain').then(r => r.json()).then(g => {
  nodes = g.nodes; edges = g.edges;
  const counts = {};
  nodes.forEach(n => counts[n.group] = (counts[n.group] || 0) + 1);
  const groups = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  groups.forEach((gr, i) => color[gr] = PALETTE[i % PALETTE.length]);
  nodes.forEach(n => {
    byId.set(n.id, n); adj.set(n.id, new Set());
    const a = groups.indexOf(n.group) / groups.length * 2 * Math.PI + Math.random() * .9, r = 80 + Math.random() * 220;
    Object.assign(n, {x: Math.cos(a) * r, y: Math.sin(a) * r, vx: 0, vy: 0, r: 3.5 + Math.sqrt(n.degree) * 2.3, phase: Math.random() * 6.28});
  });
  edges.forEach(e => {
    e.s = byId.get(e.source); e.t = byId.get(e.target); e.phase = Math.random();
    adj.get(e.source).add(e.target); adj.get(e.target).add(e.source);
  });
  const recalled = nodes.filter(n => n.touched).length;
  document.getElementById('stats').textContent =
    `${nodes.length} notes, ${edges.length} connections` + (recalled ? `, ${recalled} recalled by the agent lately` : '');
  document.getElementById('legend').innerHTML =
    groups.map(gr => `<span><i style="background:${color[gr]}"></i>${esc(gr)} <b>${counts[gr]}</b></span>`).join('') +
    '<br>' + Object.entries(EDGE).map(([k, [c]]) => `<span><i style="background:${c};border-radius:1px;height:2px;width:14px"></i>${k}</span>`).join('');
  resize(); requestAnimationFrame(frame);
  const want = decodeURIComponent(location.hash.slice(1));
  if (byId.has(want)) setTimeout(() => select(byId.get(want), true), 600);
});

function resize() {
  dpr = window.devicePixelRatio || 1; W = C.clientWidth; H = C.clientHeight;
  C.width = W * dpr; C.height = H * dpr;
}
addEventListener('resize', resize);

function tick() {
  if (alpha < .002) return;
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j]; let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy;
      if (d2 > 160000) continue;
      if (d2 < 1) { dx = Math.random() - .5; dy = Math.random() - .5; d2 = 1; }
      const f = -140 * alpha / d2;
      a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
    }
  }
  for (const e of edges) {
    const len = EDGE[e.kind][2], k = e.kind === 'mention' ? .15 : .5;
    const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = (d - len) / d * alpha * k / Math.max(1, Math.min(adj.get(e.source).size, adj.get(e.target).size));
    e.t.vx -= dx * f; e.t.vy -= dy * f; e.s.vx += dx * f; e.s.vy += dy * f;
  }
  for (const n of nodes) {
    n.vx -= n.x * .012 * alpha; n.vy -= n.y * .012 * alpha;
    if (drag && n === drag.node) { n.vx = n.vy = 0; continue; }
    n.vx *= .6; n.vy *= .6; n.x += n.vx; n.y += n.vy;
  }
  alpha += (0 - alpha) * .0228;
}

function focusSet() {
  const f = hover || selected;
  if (!f) return null;
  const s = new Set(adj.get(f.id)); s.add(f.id); return s;
}

function frame(t) {
  tick();
  X.setTransform(dpr, 0, 0, dpr, 0, 0); X.clearRect(0, 0, W, H);
  X.translate(W / 2 + view.x, H / 2 + view.y); X.scale(view.k, view.k);
  const near = focusSet(), f = hover || selected;
  for (const e of edges) {
    const [c, a] = EDGE[e.kind], lit = f && (e.s === f || e.t === f);
    X.globalAlpha = near ? (lit ? .95 : .04) : matches ? .06 : a;
    X.strokeStyle = c; X.lineWidth = (lit ? 1.6 : 1) / view.k;
    X.setLineDash(e.kind === 'mention' ? [4 / view.k, 4 / view.k] : []);
    X.beginPath(); X.moveTo(e.s.x, e.s.y); X.lineTo(e.t.x, e.t.y); X.stroke();
  }
  X.setLineDash([]);
  // Signals travel out along the edges of notes the agent read or wrote lately.
  for (const e of edges) {
    if (!(e.s.touched || e.t.touched) || (near && !(near.has(e.s.id) && near.has(e.t.id)))) continue;
    const p = (t / 2200 + e.phase) % 1, from = e.s.touched ? e.s : e.t, to = from === e.s ? e.t : e.s;
    X.globalAlpha = Math.sin(p * Math.PI) * .9; X.fillStyle = '#fff6c8';
    X.beginPath(); X.arc(from.x + (to.x - from.x) * p, from.y + (to.y - from.y) * p, 1.8 / view.k + .6, 0, 7); X.fill();
  }
  for (const n of nodes) {
    const dim = (near && !near.has(n.id)) || (matches && !matches.has(n.id));
    const fresh = n.age_days < 2 ? 1 : n.age_days < 14 ? .5 : 0;
    const pulse = fresh ? .6 + .4 * Math.sin(t / 700 + n.phase) : 0;
    X.globalAlpha = dim ? .15 : 1;
    X.shadowColor = color[n.group];
    X.shadowBlur = dim ? 0 : 6 + (fresh * 18 + Math.min(n.touched, 5) * 4) * pulse + (n === f ? 18 : 0);
    X.fillStyle = color[n.group];
    X.beginPath(); X.arc(n.x, n.y, n.r, 0, 7); X.fill();
    X.shadowBlur = 0;
    if (n.touched && !dim) {
      X.strokeStyle = '#fff6c8'; X.lineWidth = 1.4 / view.k;
      X.beginPath(); X.arc(n.x, n.y, n.r + 3 / view.k, 0, 7); X.stroke();
    }
  }
  X.textAlign = 'center'; X.textBaseline = 'top';
  X.font = `${11 / view.k}px system-ui, sans-serif`;
  for (const n of nodes) {
    const show = near ? near.has(n.id) : matches ? matches.has(n.id) : n.degree >= 6 || n.r * view.k > 11;
    if (!show) continue;
    X.globalAlpha = n === f ? 1 : .85; X.fillStyle = '#e6e9f0';
    const label = n.title.length > 34 ? n.title.slice(0, 32) + '…' : n.title;
    X.fillText(label, n.x, n.y + n.r + 3 / view.k);
  }
  X.globalAlpha = 1;
  requestAnimationFrame(frame);
}

const world = (sx, sy) => [(sx - W / 2 - view.x) / view.k, (sy - H / 2 - view.y) / view.k];
function nodeAt(sx, sy) {
  const [x, y] = world(sx, sy);
  let best = null, bd = Infinity;
  for (const n of nodes) {
    const d = Math.hypot(n.x - x, n.y - y);
    if (d < n.r + 6 / view.k && d < bd) { best = n; bd = d; }
  }
  return best;
}

C.addEventListener('pointerdown', ev => {
  C.setPointerCapture(ev.pointerId);
  drag = {node: nodeAt(ev.offsetX, ev.offsetY), sx: ev.offsetX, sy: ev.offsetY, moved: false};
  C.style.cursor = 'grabbing';
});
C.addEventListener('pointermove', ev => {
  if (drag) {
    const dx = ev.offsetX - drag.sx, dy = ev.offsetY - drag.sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (drag.node) { [drag.node.x, drag.node.y] = world(ev.offsetX, ev.offsetY); alpha = Math.max(alpha, .25); }
    else { view.x += dx; view.y += dy; }
    drag.sx = ev.offsetX; drag.sy = ev.offsetY;
    return;
  }
  hover = nodeAt(ev.offsetX, ev.offsetY);
  C.style.cursor = hover ? 'pointer' : 'grab';
  if (!hover) { tip.hidden = true; return; }
  tip.hidden = false;
  tip.innerHTML = `<strong>${esc(hover.title)}</strong><br><span class="muted">${esc(hover.id)}<br>` +
    `edited ${ago(hover.age_days)}, ${hover.degree} connections` + (hover.touched ? `, recalled ${hover.touched}x` : '') + '</span>';
  tip.style.left = Math.max(0, Math.min(ev.offsetX + 14, W - 330)) + 'px'; tip.style.top = (ev.offsetY + 14) + 'px';
});
C.addEventListener('pointerup', () => {
  if (drag && !drag.moved) drag.node ? select(drag.node) : closePanel();
  drag = null; C.style.cursor = hover ? 'pointer' : 'grab';
});
C.addEventListener('pointerleave', () => { hover = null; tip.hidden = true; });
C.addEventListener('wheel', ev => {
  ev.preventDefault();
  const k = Math.min(6, Math.max(.2, view.k * Math.exp(-ev.deltaY * .0015)));
  const ox = ev.offsetX - W / 2, oy = ev.offsetY - H / 2;
  view.x = ox - (ox - view.x) * k / view.k; view.y = oy - (oy - view.y) * k / view.k; view.k = k;
}, {passive: false});
C.addEventListener('dblclick', () => { view = {x: 0, y: 0, k: 1}; });

// Centre in the part of the canvas the panel doesn't cover.
function center(n) { view.x = -n.x * view.k - (panel.hidden ? 0 : panel.offsetWidth / 2); view.y = -n.y * view.k; }

async function select(n, recenter) {
  selected = n; panel.hidden = false;
  if (recenter) center(n);
  history.replaceState(null, '', '#' + encodeURIComponent(n.id));
  noteEl.innerHTML = '<p class="muted">Loading…</p>';
  const r = await fetch('/api/brain/note?id=' + encodeURIComponent(n.id));
  if (selected !== n) return;
  if (!r.ok) { noteEl.innerHTML = '<p class="bad">Could not load this note.</p>'; return; }
  const d = await r.json();
  const rel = (label, list) => list.length ? `<h2>${label}</h2><ul class="rel">` + list.map(o =>
    `<li><a href="#" data-note="${esc(o.id)}">${esc(o.title)}</a> <span class="kind">${o.kind}</span></li>`).join('') + '</ul>' : '';
  noteEl.innerHTML = `<h3>${esc(n.title)}</h3><div class="meta">${esc(n.id)}<br>edited ${ago(n.age_days)}, ` +
    `${n.words} words` + (n.touched ? `, recalled by the agent ${n.touched}x` : '') +
    (d.url ? ` · <a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">open in Notion</a>` : '') + '</div>' +
    rel('Links to', d.out) + rel('Linked from', d.back) + (d.editable
      ? `<div class="body file" data-path="${esc(n.id)}" data-hash="${esc(d.hash)}"><div class="filehead">` +
        `<button type="button" data-edit>Edit</button><span class="msg"></span></div><div class="view">${d.html}</div>` +
        `<div class="editor" hidden><textarea spellcheck="false"></textarea><button type="button" data-save>Save</button> ` +
        `<button type="button" data-cancel>Cancel</button> <span class="muted">Ctrl+S saves</span></div></div>`
      : `<div class="body">${d.html}</div>` + (d.url ? '<p class="muted">Mirrored from Notion: edit it there.</p>' : ''));
  const ta = noteEl.querySelector('textarea');
  if (ta) ta.value = d.raw;
}
function closePanel() { selected = null; panel.hidden = true; history.replaceState(null, '', location.pathname); }
document.getElementById('close').onclick = closePanel;
noteEl.addEventListener('click', ev => {
  const a = ev.target.closest('[data-note]');
  if (!a) return;
  ev.preventDefault();
  const n = byId.get(a.dataset.note);
  if (n) select(n, true);
});

const q = document.getElementById('q');
q.addEventListener('input', () => {
  const s = q.value.trim().toLowerCase();
  matches = s ? new Set(nodes.filter(n => n.title.toLowerCase().includes(s) || n.id.toLowerCase().includes(s)).map(n => n.id)) : null;
});
q.addEventListener('keydown', ev => {
  if (ev.key === 'Enter' && matches && matches.size) select(byId.get([...matches][0]), true);
  if (ev.key === 'Escape') { q.value = ''; matches = null; }
});
"""
