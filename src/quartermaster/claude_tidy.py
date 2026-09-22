"""Keeping the agent's own Notion page from rotting.

The Claude page is written freely and never pruned, so it fills with things that
were true once. Weekly, this reads it, has one model call rewrite it without the
stale parts, and *proposes* the result (``notion_writes``) rather than applying
it: a whole-page replace is exactly the change that should not happen while
nobody is looking. The owner gets Confirm/Cancel in Discord, and the previous
contents are saved into the vault's git before anything is overwritten.

The model is asked to cut and merge, never to invent: anything it is unsure
about stays.
"""

from __future__ import annotations

import asyncio
import logging

from . import agent, db, notion_writes
from .config import Settings
from .integrations import claude_page
from .integrations.notion import NotionClient

log = logging.getLogger(__name__)

NO_CHANGES = "NO CHANGES"
MIN_CHARS = 200  # below this there is nothing worth tidying

PROMPT = """This is the "Claude" page in the owner's Notion: the one page their \
assistant writes to freely. It accumulates, and nothing prunes it.

Rewrite it as it should be now:
- Drop what is stale: finished to-dos, dated plans that have passed, notes about \
things that already happened, anything superseded by a later entry.
- Merge duplicates and near-duplicates into one entry.
- Keep everything still useful, in the owner's own words where possible, and keep \
the page's existing structure and headings.
- Never invent anything, and never drop something you are unsure about. When in \
doubt, keep it.
- Keep dated entries that are still upcoming.

Today is {today}.

Reply with the complete rewritten page in markdown, and nothing else - no \
preamble, no explanation. If nothing should change, reply with exactly {sentinel}.

--- PAGE ---
{page}
"""


def run_tidy(settings: Settings, *, dry_run: bool = False) -> str:
    """Propose a tidied Claude page. Returns what happened, for the CLI."""
    from datetime import date

    page_id = claude_page._root(settings)  # raises if not configured
    with NotionClient(settings.notion_token or "") as client:
        current = client.page_markdown(page_id).markdown
    if len(current.strip()) < MIN_CHARS:
        return "Claude page is short; nothing to tidy."

    prompt = PROMPT.format(today=date.today().isoformat(), sentinel=NO_CHANGES, page=current)
    reply = asyncio.run(agent.ask(prompt, agent.tidy_profile(settings), settings.claude_cli))
    if not reply.ok:
        raise RuntimeError(reply.error or "tidy produced no reply")

    cleaned = reply.text.strip()
    if cleaned.startswith("```"):  # a fenced page, despite the instruction
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    if not cleaned or cleaned == NO_CHANGES or cleaned == current.strip():
        return "Claude page looks fine; nothing proposed."
    if len(cleaned) < len(current.strip()) * 0.4:
        # A tidy should prune, not gut. This is the failure mode worth catching
        # in code rather than trusting the owner to spot in a preview.
        log.warning("tidy would cut the Claude page from %d to %d chars; not proposing",
                    len(current), len(cleaned))
        return "Tidy would have removed more than half the page; left alone."

    saved = f"{len(current)} -> {len(cleaned)} characters"
    if dry_run:
        return f"Would propose a tidy ({saved}):\n\n{cleaned[:1500]}"

    with db.session(settings.db_path) as conn:
        write_id = notion_writes.propose(
            conn, page_id, "Claude", "replace", cleaned,
            why=f"Weekly tidy of the Claude page ({saved}).", source="tidy",
        )
    log.info("proposed Claude page tidy as write %s (%s)", write_id, saved)
    return f"Proposed tidy #{write_id} ({saved}). Confirm it in Discord."
