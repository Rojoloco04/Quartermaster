"""Resume changes from a DM: proposed by the owner agent, confirmed by the owner.

The owner agent can't reach the portfolio repo (its files are outside the
vault). It reads the resume through ``read_resume`` and proposes exact-text
edits to resume.tex and site.toml through ``propose_resume_edit``. That only
stores a ``pending_writes`` row (``mode = 'resume'``), after proving the edit
parses, renders and still builds to one page. The bot DMs the owner a preview
built in code from the row, with Confirm/Cancel, like a Notion proposal; on
Confirm, code writes the files and publishes (PDF, site, commit, push).

The gate matters more here than for Notion: the result goes on a public
website. An email the agent read can produce a proposal, never a publish.

The preview shows, for every entry whose site text would need re-checking,
what site.toml says about it. Pressing Confirm is that review.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from .. import db
from ..config import Settings
from . import build, publish, site, tex
from .tex import ResumeError


log = logging.getLogger(__name__)

MODE = "resume"
FILES = ("resume.tex", "site.toml")
PREVIEW_CHARS = 1900  # one Discord message, with room for the result line


def read(settings: Settings) -> str:
    r = publish.repo(settings)
    parts = []
    for name in FILES:
        text, _ = publish.read_text(r.tex_dir / name)
        parts.append(f"===== {name} =====\n{text}")
    problems = publish.check(settings)
    if problems:
        parts.append("===== out of sync right now =====\n" + "\n".join(f"- {p}" for p in problems))
    return "\n\n".join(parts)


def edited(current: dict[str, str], edits: list[dict]) -> dict[str, str]:
    """Apply exact-text replacements. Each ``old`` must appear exactly once in
    its file, so an edit can't land somewhere other than where it was aimed."""
    files = dict(current)
    if not edits:
        raise ResumeError("No edits given.")
    for n, edit in enumerate(edits, 1):
        name, old, new = edit.get("file", "resume.tex"), edit.get("old", ""), edit.get("new", "")
        if name not in files:
            raise ResumeError(f"edit {n}: file must be one of {', '.join(FILES)}")
        if not old:
            raise ResumeError(f"edit {n}: old is empty; quote the exact text to replace (read_resume shows it)")
        count = files[name].count(old)
        if count != 1:
            raise ResumeError(f"edit {n}: the old text appears {count} times in {name}; it must appear exactly "
                              "once. Quote it exactly, with enough around it to be unique.")
        files[name] = files[name].replace(old, new)
    if files == current:
        raise ResumeError("The edits change nothing.")
    return files


def propose(settings: Settings, conn: sqlite3.Connection, edits: list[dict], why: str = "",
            source: str = "agent") -> int:
    r = publish.repo(settings)
    current = {name: publish.read_text(r.tex_dir / name)[0] for name in FILES}
    files = edited(current, edits)
    resume = tex.parse(files["resume.tex"])
    site.render(resume, site.load_extras(files["site.toml"]))
    build.build(resume, r.tex_dir, source=files["resume.tex"])  # one page, text intact
    content = json.dumps({"files": {n: {"old": current[n], "new": files[n]} for n in FILES if files[n] != current[n]}})
    cur = conn.execute(
        """
        INSERT INTO pending_writes (page_id, page_title, mode, content, why, source, status, created_at)
        VALUES ('resume', 'Resume', ?, ?, ?, ?, 'pending', ?)
        """,
        (MODE, content, why, source, db.utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def _versions(row: sqlite3.Row) -> tuple[dict[str, str], dict[str, str]]:
    changed = json.loads(row["content"])["files"]
    return ({n: v["old"] for n, v in changed.items()}, {n: v["new"] for n, v in changed.items()})


def preview(row: sqlite3.Row, settings: Settings | None = None) -> str:
    """What the owner reads before pressing anything: built from the row's
    files in code, never from the model's prose."""
    old_files, new_files = _versions(row)
    try:
        r = publish.repo(settings) if settings else None
        current = {n: publish.read_text(r.tex_dir / n)[0] for n in FILES} if r else {}
        old_all, new_all = {**current, **old_files}, {**current, **new_files}
        old_resume, new_resume = tex.parse(old_all["resume.tex"]), tex.parse(new_all["resume.tex"])
        extras = site.load_extras(new_all["site.toml"])
        reviewed = site.load_reviewed(r.reviewed) if r else {}
    except (ResumeError, OSError) as exc:
        return f"**Resume change #{row['id']}**\n_Can't show it: {exc}_"

    lines = [f"**Resume change #{row['id']}**"]
    if row["why"]:
        lines.append(f"_{row['why']}_")
    body: list[str] = []
    for change in tex.diff(old_resume, new_resume):
        body.append(f"@@ {change.id} ({change.kind})")
        body += change.lines
    if "site.toml" in new_files:
        import difflib

        site_lines = [ln for ln in difflib.unified_diff(
            old_files["site.toml"].splitlines(), new_files["site.toml"].splitlines(), lineterm="", n=0)
            if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
        body += ["@@ site.toml"] + site_lines
    if body:
        lines.append("```diff\n" + "\n".join(body) + "\n```")
    to_check = site.stale(new_resume, extras, reviewed)
    if to_check:
        lines.append("**Still right on the site?** Confirming says yes:")
        for entry in to_check:
            lines.append(f"- {entry.id}: " + " / ".join(site.site_text(entry, extras)))
    lines.append("_On Confirm: builds the PDF, updates the site, commits and pushes the portfolio._")
    text = "\n".join(lines)
    if len(text) > PREVIEW_CHARS:
        text = text[: PREVIEW_CHARS - 40].rstrip() + "\n… (cut; the full change is in the log)```"
        log.info("resume change #%s preview (full): %s", row["id"], "\n".join(lines))
    return text


def apply(settings: Settings, conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Write the confirmed files and publish. If anything fails, the files are
    put back as they were and nothing is committed."""
    from ..knowledge.notion_writes import decide

    old_files, new_files = _versions(row)
    written: dict[str, str] = {}
    try:
        r = publish.repo(settings)
        extras = r.extras()
        publish._preflight(r)
        dirty = publish._dirty(r, r.paths(extras))
        if dirty:
            raise ResumeError("the portfolio has uncommitted changes in " + ", ".join(dirty)
                              + "; publish or discard them first")
        publish.catch_up(r)
        for name, old in old_files.items():
            if publish.read_text(r.tex_dir / name)[0] != old:
                raise ResumeError(f"{name} changed since this was proposed; ask for it again")
        for name, new in new_files.items():
            publish.write_text(r.tex_dir / name, new)
            written[name] = old_files[name]
        result = publish.publish(settings, review_all=True, message=row["why"] or "")
    except Exception as exc:  # noqa: BLE001 - report to the owner, never crash the bot
        log.exception("applying resume change %s failed", row["id"])
        for name, old in written.items():
            try:
                publish.write_text(publish.repo(settings).tex_dir / name, old)
            except Exception:  # noqa: BLE001
                log.exception("restoring %s after a failed resume change", name)
        decide(conn, row["id"], "failed")
        return f"❌ Couldn't apply resume change #{row['id']}: {exc}"
    decide(conn, row["id"], "applied")
    log.info("applied resume change %s", row["id"])
    return f"✅ {result}"
