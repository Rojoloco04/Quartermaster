"""Command line entry point.

    qm doctor   what's configured, what's missing, what's broken
    qm init     create the vault from the template
    qm sync     pull Notion into the vault
    qm bot      run the Discord bot
    qm mute     silence something permanently
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from . import db, mutes
from .config import REPO_ROOT, load_settings

TEMPLATE = REPO_ROOT / "vault-template"

OK = "  ok   "
WARN = " warn  "
FAIL = " FAIL  "


def _find_claude() -> str | None:
    """Locate the Claude Code CLI.

    Preference order matters. A binary on PATH is stable; the copy bundled in
    the VSCode extension sits behind a version number and moves every time the
    extension updates, which would break a long-running bot silently.
    """
    found = shutil.which("claude")
    if found:
        return found

    ext_root = Path.home() / ".vscode" / "extensions"
    candidates = sorted(ext_root.glob("anthropic.claude-code-*/resources/native-binary/claude.exe"))
    return str(candidates[-1]) if candidates else None


def cmd_doctor(args: argparse.Namespace) -> int:
    print("Quartermaster doctor\n")
    problems = 0

    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"[{FAIL}] config: {exc}")
        return 1

    print(f"[{OK}] repo        {REPO_ROOT}")

    if settings.vault.exists():
        print(f"[{OK}] vault       {settings.vault}")
        if not (settings.vault / "CLAUDE.md").exists():
            print(f"[{WARN}] vault has no CLAUDE.md - run 'qm init'")
            problems += 1
    else:
        print(f"[{WARN}] vault       {settings.vault} (does not exist - run 'qm init')")
        problems += 1

    claude = settings.claude_cli or _find_claude()
    if not claude:
        print(f"[{FAIL}] claude cli  not found. The Agent SDK needs it.")
        print("              fix: npm install -g @anthropic-ai/claude-code")
        problems += 1
    elif ".vscode" in claude:
        print(f"[{WARN}] claude cli  {claude}")
        print("              This is the VSCode extension's private copy. Its path")
        print("              changes on every extension update, which will break the")
        print("              bot without warning. Install a stable one:")
        print("              npm install -g @anthropic-ai/claude-code")
        problems += 1
    else:
        print(f"[{OK}] claude cli  {claude}")

    creds = Path.home() / ".claude" / ".credentials.json"
    if creds.exists():
        import json

        try:
            data = json.loads(creds.read_text(encoding="utf-8"))
            sub = (data.get("claudeAiOauth") or {}).get("subscriptionType")
            if sub:
                print(f"[{OK}] auth        subscription ({sub}) - not billed per token")
            else:
                print(f"[{WARN}] auth        credentials present but no subscription found")
        except Exception:
            print(f"[{WARN}] auth        credentials file unreadable")
    else:
        print(f"[{WARN}] auth        no ~/.claude/.credentials.json - run 'claude' once to log in")
        problems += 1

    secrets = [
        ("NOTION_TOKEN", settings.notion_token, "phase 1 - notion mirror"),
        ("NOTION_CLAUDE_PAGE_ID", settings.notion_claude_page_id, "phase 1 - writable page"),
        ("DISCORD_BOT_TOKEN", settings.discord_bot_token, "phase 2 - the bot"),
    ]
    print()
    for name, value, why in secrets:
        mark = OK if value else WARN
        state = "set" if value else f"not set ({why})"
        print(f"[{mark}] {name:<22} {state}")

    if settings.db_path.exists():
        with db.session(settings.db_path) as conn:
            pages = conn.execute(
                "SELECT COUNT(*) c FROM notion_pages WHERE archived = 0"
            ).fetchone()["c"]
        print(f"\n[{OK}] state.db    {pages} mirrored pages")
    else:
        print(f"\n[{WARN}] state.db    not created yet (run 'qm sync')")

    print("\n" + ("All good." if problems == 0 else f"{problems} thing(s) need attention."))
    return 0 if problems == 0 else 1


def cmd_init(args: argparse.Namespace) -> int:
    settings = load_settings()
    vault = settings.vault

    if vault.exists() and any(vault.iterdir()) and not args.force:
        print(f"{vault} already exists and is not empty.")
        print("Nothing changed. Re-run with --force to copy template files over it.")
        return 1

    vault.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in TEMPLATE.rglob("*"):
        if src.is_dir():
            continue
        dest = vault / src.relative_to(TEMPLATE)
        if dest.exists() and not args.force:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1

    print(f"Vault ready at {vault} ({copied} files).")

    if not (vault / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=vault, check=False)
        (vault / ".gitignore").write_text(
            "# Machine state - rebuildable, and noisy in diffs.\n"
            "90-System/state.db\n90-System/state.db-wal\n90-System/state.db-shm\n",
            encoding="utf-8",
        )
        print("Initialised a git repo in the vault. Add your private GitHub remote when ready.")

    db.connect(settings.db_path).close()
    print("Created state.db.")
    print("\nNext: put NOTION_TOKEN in .env, then run 'qm sync'.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from .notion_sync import sync

    settings = load_settings()
    if not settings.vault.exists():
        print("No vault yet. Run 'qm init' first.")
        return 1

    print("Pulling Notion...")
    stats = sync(settings, force=args.force)
    print(stats.summary())

    # Every partial outcome below is reported rather than swallowed. A quiet
    # partial sync is how the agent ends up confidently wrong about what it
    # has read.
    if stats.empty and stats.written and len(stats.empty) > stats.written / 2:
        print()
        print(f"  WARNING: {len(stats.empty)} of {stats.written} pages mirrored with no content.")
        print("  That is almost certainly a parsing bug, not that many blank pages.")
        print("  The mirror is not trustworthy until this is resolved.")
    elif stats.empty:
        print(f"  {len(stats.empty)} page(s) were blank in Notion (mirrored as empty).")

    for title in stats.truncated:
        print(f"  TRUNCATED: {title} - mirror is incomplete, see the file's frontmatter")
    for title, err in stats.failed:
        print(f"  FAILED:    {title} - {err}")

    return 1 if stats.failed else 0


def cmd_bot(args: argparse.Namespace) -> int:
    from .surfaces.discord_bot import run

    return run(load_settings())


def cmd_mute(args: argparse.Namespace) -> int:
    settings = load_settings()
    added = mutes.add(settings.muted_file, args.item_id, args.summary or "", args.reason or "")
    if added:
        print(f"Muted {args.item_id}. It won't be raised again.")
    else:
        print(f"{args.item_id} was already muted.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qm", description="Quartermaster")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check configuration and dependencies").set_defaults(
        func=cmd_doctor
    )

    p_init = sub.add_parser("init", help="create the vault from the template")
    p_init.add_argument("--force", action="store_true", help="overwrite existing template files")
    p_init.set_defaults(func=cmd_init)

    p_sync = sub.add_parser("sync", help="pull Notion into the vault")
    p_sync.add_argument("--force", action="store_true", help="refetch every page, ignoring cache")
    p_sync.set_defaults(func=cmd_sync)

    sub.add_parser("bot", help="run the Discord bot").set_defaults(func=cmd_bot)

    p_mute = sub.add_parser("mute", help="permanently silence an item")
    p_mute.add_argument("item_id", help="e.g. stale:abc123 or event:artist/Tool")
    p_mute.add_argument("--summary", help="human-readable label")
    p_mute.add_argument("--reason", help="why, for future reference")
    p_mute.set_defaults(func=cmd_mute)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
