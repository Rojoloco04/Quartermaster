"""Commit everything in the vault and push it to its private remote.

The remote is the vault's backup (restic was dropped: git already keeps every
version, off this machine). Run daily by the scheduler, so at most a day of
what the agent learns is ever only on this disk. Nothing to commit and nothing
unpushed sends nothing.

Never forces, never skips hooks: a rejected push or a failing hook is reported
and left for a person. A merge or rebase in progress is left alone too, since
committing into one would finish it with whatever happens to be staged.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections import Counter
from pathlib import Path

log = logging.getLogger(__name__)

PUSH_TIMEOUT_SECONDS = 120
MAX_BODY_FILES = 50

# Unattended: a credential prompt would hang the task forever, so fail instead.
_NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


def _git(vault: Path, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        env={**os.environ, **_NO_PROMPT},
    )


def _ok(vault: Path, *args: str, timeout: float | None = None) -> str:
    result = _git(vault, *args, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def summarize(paths: list[str]) -> str:
    """'notion 20, facts 2, CLAUDE.md' - changed files counted by top-level
    folder, biggest first. Pure, for tests."""
    folders = Counter(p.split("/", 1)[0] for p in paths if "/" in p)
    parts = [f"{name} {n}" for name, n in folders.most_common()]
    return ", ".join(parts + [p for p in paths if "/" not in p])


def run_push(vault: Path) -> str:
    git_dir = vault / ".git"
    if not git_dir.is_dir():
        raise RuntimeError(f"{vault} is not a git repository")
    for marker in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"):
        if (git_dir / marker).exists():
            raise RuntimeError(f"a git operation is in progress in the vault ({marker}); finish it by hand")
    if _git(vault, "symbolic-ref", "-q", "HEAD").returncode != 0:
        raise RuntimeError("the vault's HEAD is detached; check out a branch by hand")

    _ok(vault, "add", "-A")
    changed = [line for line in _ok(vault, "diff", "--cached", "--name-only", "-z").split("\0") if line]
    committed = ""
    if changed:
        body = "\n".join(changed[:MAX_BODY_FILES])
        if len(changed) > MAX_BODY_FILES:
            body += f"\n... and {len(changed) - MAX_BODY_FILES} more"
        committed = f"{len(changed)} file(s): {summarize(changed)}"
        _ok(vault, "commit", "-q", "-m", f"qm push: {summarize(changed)}", "-m", body)

    ahead = _git(vault, "rev-list", "--count", "@{u}..HEAD")
    if ahead.returncode != 0:
        raise RuntimeError("the vault's branch has no upstream; run `git push -u origin <branch>` once by hand")
    if int(ahead.stdout.strip() or 0) == 0:
        return "Nothing to push."
    _ok(vault, "push", "-q", timeout=PUSH_TIMEOUT_SECONDS)
    summary = f"Committed {committed} and pushed." if committed else f"Pushed {ahead.stdout.strip()} earlier commit(s)."
    log.info("vault push: %s", summary)
    return summary
