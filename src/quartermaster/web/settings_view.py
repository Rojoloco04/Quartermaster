"""``/settings``: every preference in force, which secrets are set, and
in-place editors for the files in ``EDITABLE`` - the only files ``qm web``
writes. A save is refused if the file changed since it was loaded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tomllib
from pathlib import Path

from ..config import _deep_merge, DEFAULTS, REPO_ROOT, Settings
from .layout import _e, clip, markdown_to_html, page


log = logging.getLogger(__name__)


# --- Editing (the only writes qm web makes) ------------------------------------
#
# Everything that configures Quartermaster or holds what it knows, so the owner
# never has to open the vault to check or change it. Not the Notion mirror (the
# next sync overwrites it; edit in Notion), and not digests or the inbox.

EDITABLE = ("CLAUDE.md", "System/config.toml", "System/muted.md", "System/dev-queue.md",
            "System/conflicts.md", "System/minecraft-links.md", "System/tone.md")
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
    path = vault / "System" / "config.toml"
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
    path = vault / "System" / "config.toml"
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
    return save_file(vault, "System/config.toml", text, loaded_hash)


def secret_status() -> list[tuple[str, bool]]:
    """(name, set?) for every key in .env.example. Never the values."""
    example = REPO_ROOT / ".env.example"
    names = re.findall(r"^([A-Z][A-Z0-9_]+)=", example.read_text("utf-8"), re.M) if example.exists() else []
    return [(name, bool(os.getenv(name))) for name in dict.fromkeys(names)]


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
    """An editable file. Live-refreshed (``layout.LIVE_JS``) while its editor is
    closed, so a write by the agent shows up, with the hash a save needs."""
    path = editable_path(vault, rel)
    text = path.read_text("utf-8") if path and path.exists() else ""
    view = render_file(rel, text) if text.strip() else "<p class='muted'>Nothing here yet.</p>"
    block_id = "file-" + re.sub(r"[^\w-]", "-", rel)
    return (
        f"<div class='file' id='{block_id}' data-live data-path='{_e(rel)}' data-hash='{file_hash(path) if path else ''}'>"
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
    "facts/interests.md": "Filters the digest's events and on-sales. A line you write outranks anything inferred.",
}


def _fact_title(path: Path) -> str:
    if not path.exists():
        return "Lessons"
    m = re.search(r"^# (.+)$", path.read_text("utf-8"), re.M)
    return m[1].strip() if m else path.stem


def settings_page(settings: Settings, csrf: str) -> str:
    vault = settings.vault
    prefs = "".join(
        f"<tr data-key='{_e(key)}'><td class='nw'><code>{_e(key)}</code></td>"
        + (f"<td class='v'>{clip(value, 90)}</td>" if value.startswith(("[", "{")) or key.count(".") != 1
           else f"<td class='v' data-pref title='Click to change'>{_e(value)}</td>")
        + "</tr>"
        for key, value, _ in effective_prefs(vault)
    )
    config_hash = file_hash(vault / "System" / "config.toml")
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
<section><h2>Preferences in force</h2><table id="prefs" data-live data-hash="{config_hash}"><tr><th>Setting</th><th>Value</th></tr>{prefs}</table>
{file_block(vault, "System/config.toml", "Edit preferences", "The whole file, for lists like the distance bands. Saved only if it parses.")}</section>
<section><h2>Conflicts</h2>{file_block(vault, "System/conflicts.md", "Where what it knows disagrees", "Found by the daily reconcile (<code>qm reconcile</code>). Answer in a DM and every file gets updated, or fix it yourself and delete the entry.")}</section>
<section><h2>What it knows</h2>{fact_blocks}</section>
<section><h2>Tone</h2>{file_block(vault, "System/tone.md", "How it talks", "In DMs, the web chat and server channels; not the digest. Set from a DM (\"change your tone to an angry grandma\", \"back to normal\"). Swearing is always allowed.")}</section>
<section><h2>Instructions</h2>{file_block(vault, "CLAUDE.md", "How the agent works in your vault", "Loaded at the start of every conversation and into every digest.")}</section>
<div class="grid2">
<section><h2>Mutes</h2>{file_block(vault, "System/muted.md", "Never raise these again", "One <code>kind:key</code> per line. <code>artist/Tool</code> with no kind mutes every kind.")}</section>
<section><h2>Dev queue</h2>{file_block(vault, "System/dev-queue.md", "Changes to Quartermaster itself", "Worked in Claude Code. Delete an item's line to close it.")}</section>
</div>
<section><h2>Minecraft links</h2>{file_block(vault, "System/minecraft-links.md", "Discord accounts linked to Minecraft names", "A linked name on the server's op list may start, stop and run commands from a Discord channel. Added when someone proves both accounts in-game; delete a line to unlink.")}</section>
<section><h2>Secrets</h2><p class="note">In the repo's <code>.env</code>. Shown as set or not, never their values, and not editable from a browser.</p>
<table id="secrets" data-live>{env}</table></section>""", EDIT_JS + PREF_JS, EDIT_CSS + PREF_CSS, csrf)
