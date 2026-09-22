"""Keeping what the agent knows consistent: one source of truth per fact.

Facts get written from many conversations, so they drift: the same thing in two
files, a plan that already happened, "still has to buy tickets" in one file and
"already going" in another. Daily, one model call reads every ``facts/`` file
(lessons included) beside the Notion mirror and returns:

- **edits** to facts files that remove bloat without changing meaning: merged
  duplicates, dated items that have passed, statements Notion or a newer fact
  plainly supersede. Code applies them, because facts are the agent's own
  memory and asking about every dedupe is the friction this exists to remove.
  Each edited file's previous version is kept in ``90-System/backups/``; a file
  changed while the model was thinking is skipped; a rewrite that cuts more
  than 60% is dropped (it prunes, it doesn't gut).
- **conflicts** it can't settle: two sources disagree and nothing says which is
  current. Those are the owner's to answer, so they're written to
  ``90-System/conflicts.md`` (shown on /settings, and in every owner turn's
  context) and DM'd as a question. The owner answers in plain words and the
  owner agent updates every file that states it.

The model has no tools and only returns data: an instruction smuggled into a
fact (facts can come from email) can at worst propose an edit to another facts
file, inside the same guards.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import date, datetime
from pathlib import Path

from . import agent
from .config import Settings

log = logging.getLogger(__name__)

MAX_CUT = 0.6  # a rewrite may remove at most this share of a file
MIN_PAGE_WORDS = 3  # empty Notion pages add tokens and nothing else
_FACT = re.compile(r"facts/[\w.-]+\.md")
_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.S)

CONFLICTS_HEADER = """# Knowledge conflicts

Places where what Quartermaster knows disagrees with itself, found by the daily
reconcile. Answer in a DM ("I already have the tickets") and every file gets
updated; or fix the files and delete the entry here.

"""

SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "facts/<name>.md, exactly as given"},
                    "content": {"type": "string", "description": "the complete new file"},
                    "summary": {"type": "string", "description": "one line: what changed and why"},
                },
                "required": ["file", "content", "summary"],
            },
        },
        "conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "about": {"type": "string", "description": "a few words naming the topic"},
                    "claims": {"type": "array", "items": {"type": "string"},
                               "description": "each disagreeing claim, prefixed with its file"},
                    "question": {"type": "string", "description": "one question the owner can answer in a line"},
                },
                "required": ["about", "claims", "question"],
            },
        },
    },
    "required": ["edits", "conflicts"],
}

PROMPT = """You maintain the long-term memory of the owner's personal assistant. \
Below are its facts files (its own notes about the owner, including lessons: rules \
the owner gave when correcting it) and a mirror of the owner's Notion workspace \
(written by the owner; the source of truth for anything it states).

Today is {today}.

Return edits and conflicts:

edits - rewrite a facts file only to remove bloat, never to add knowledge:
- Merge duplicates and near-duplicates, within a file or across files. Keep the \
fact once, in the file where it fits best, in the owner's own words.
- Drop dated items whose date has passed (a plan for a day before today), and \
anything a newer statement or the Notion mirror plainly supersedes.
- Lessons (facts/lessons.md): merge duplicates; if two lessons contradict, keep the \
newer one. Never drop a lesson for any other reason.
- Keep each file's headings and structure. Never invent anything. When unsure, \
leave it. Only list files that actually change, and give the complete new file.

conflicts - where sources disagree and nothing tells you which is current \
(e.g. one file says he has tickets, another says he still needs to buy them). \
Don't edit either side; ask one short question the owner can answer in a line. \
A fact that simply isn't in Notion is not a conflict.

Return empty lists if everything is consistent.

{facts}

{notion}
"""


def conflicts_path(settings: Settings) -> Path:
    return settings.system_dir / "conflicts.md"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def gather(settings: Settings) -> tuple[dict[str, str], str]:
    """({facts id: text}, the Notion mirror as one block, empty pages skipped)."""
    facts = {
        f"facts/{p.name}": p.read_text("utf-8")
        for p in sorted(settings.facts_dir.glob("*.md")) if p.name.lower() != "readme.md"
    } if settings.facts_dir.exists() else {}
    pages = []
    if settings.notion_dir.exists():
        for p in sorted(settings.notion_dir.rglob("*.md")):
            body = _FRONTMATTER.sub("", p.read_text("utf-8", errors="replace")).strip()
            if p.name.lower() != "readme.md" and len(body.split()) >= MIN_PAGE_WORDS:
                pages.append(f"=== notion/{p.relative_to(settings.notion_dir).as_posix()} ===\n{body}")
    return facts, "\n\n".join(pages)


def build_prompt(facts: dict[str, str], notion: str, today: date) -> str:
    facts_block = "\n\n".join(f"=== {name} ===\n{text}" for name, text in facts.items()) or "(no facts files)"
    return PROMPT.format(today=today.isoformat(), facts="--- FACTS ---\n" + facts_block,
                         notion="--- NOTION MIRROR ---\n" + (notion or "(empty)"))


def apply_edits(settings: Settings, edits: list[dict], loaded: dict[str, str]) -> tuple[list[str], list[str]]:
    """Apply what's safe. Returns (applied summaries, skipped reasons)."""
    applied, skipped = [], []
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    for edit in edits:
        rel, content = str(edit.get("file", "")), str(edit.get("content", ""))
        summary = " ".join(str(edit.get("summary", "")).split()) or "tidied"
        path = settings.vault / rel
        if not _FACT.fullmatch(rel) or rel not in loaded or not path.exists():
            skipped.append(f"{rel}: not an existing facts file")
            continue
        current = path.read_text("utf-8")
        if _hash(current) != _hash(loaded[rel]):
            skipped.append(f"{rel}: changed while reconciling, left for next run")
            continue
        new = content.replace("\r\n", "\n").strip() + "\n"
        if new.strip() == current.strip():
            continue
        if len(new.strip()) < len(current.strip()) * (1 - MAX_CUT):
            log.warning("reconcile would cut %s from %d to %d chars; skipped", rel, len(current), len(new))
            skipped.append(f"{rel}: would have removed more than {int(MAX_CUT * 100)}%, left alone")
            continue
        backup = settings.system_dir / "backups" / "reconcile" / stamp / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(current, encoding="utf-8")
        path.write_text(new, encoding="utf-8")
        log.info("reconcile edited %s: %s", rel, summary)
        applied.append(f"{rel}: {summary}")
    return applied, skipped


def write_conflicts(settings: Settings, conflicts: list[dict]) -> None:
    """Replace the conflicts file with this run's list: settled ones drop off."""
    lines = []
    for c in conflicts:
        lines.append(f"## {' '.join(str(c.get('about', 'Unnamed')).split())}\n")
        lines += [f"- {' '.join(str(claim).split())}" for claim in c.get("claims") or []]
        lines.append(f"\n**Question:** {' '.join(str(c.get('question', '')).split())}\n")
    body = "\n".join(lines) if lines else f"_Nothing open (checked {date.today().isoformat()})._\n"
    path = conflicts_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFLICTS_HEADER + body, encoding="utf-8")


def for_prompt(path: Path | None) -> str:
    """Open conflicts as a system-prompt block for the owner agent, or ""."""
    text = path.read_text("utf-8") if path is not None and path.exists() else ""
    if "## " not in text:
        return ""
    body = text[text.index("## "):].strip()
    return (
        "Open knowledge conflicts (90-System/conflicts.md), already put to the owner as questions. "
        "If their message settles one, update every facts file that states it, propose a Notion edit "
        "if Notion is the wrong side, and delete that entry from 90-System/conflicts.md:\n" + body
    )


def report(applied: list[str], skipped: list[str], conflicts: list[dict]) -> str:
    """The DM, or "" when there's nothing to say."""
    parts = []
    if conflicts:
        parts.append("**Things I know that disagree** - which is right?")
        parts += [f"- {' '.join(str(c.get('question', '')).split())}" for c in conflicts]
    if applied:
        parts.append("**Tidied my notes**")
        parts += [f"- {line}" for line in applied]
    if skipped:
        parts.append("**Left alone**")
        parts += [f"- {line}" for line in skipped]
    return "\n".join(parts)


def run_reconcile(settings: Settings, *, dry_run: bool = False) -> str:
    return asyncio.run(reconcile(settings, dry_run=dry_run))


async def reconcile(settings: Settings, *, dry_run: bool = False, notify: bool = True) -> str:
    """The daily run DMs what it found (`notify`); run from a DM, the owner
    is already in the conversation, so the result is only returned."""
    facts, notion = gather(settings)
    if not facts:
        return "No facts files; nothing to reconcile."
    prompt = build_prompt(facts, notion, date.today())
    reply = await agent.ask(prompt, agent.reconcile_profile(settings, SCHEMA), settings.claude_cli)
    if not reply.ok or reply.structured is None:
        raise RuntimeError(reply.error or "reconcile returned no structured result")
    edits = list(reply.structured.get("edits") or [])
    conflicts = list(reply.structured.get("conflicts") or [])

    if dry_run:
        preview = [f"- {e.get('file')}: {e.get('summary')}" for e in edits]
        return (report([], [], conflicts) + ("\n**Would tidy**\n" + "\n".join(preview) if preview else "")).strip() \
            or "Everything is consistent."

    applied, skipped = apply_edits(settings, edits, facts)
    write_conflicts(settings, conflicts)
    message = report(applied, skipped, conflicts)
    if not message:
        return "Everything is consistent; nothing sent." if notify else "Everything is consistent."
    if notify:
        from .surfaces.digest_send import send_dm

        send_dm(settings, message)
    return message
