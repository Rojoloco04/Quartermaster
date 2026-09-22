"""Command line entry point. ``qm --help`` lists the commands.

Every command logs to ``Settings.log_path`` as well as the console, so an
unattended run (Task Scheduler, an MCP server) still leaves a record.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import db, mutes
from .agent import MCP_SERVERS
from .config import REPO_ROOT, load_settings

log = logging.getLogger("quartermaster.cli")

INTEGRATIONS = ("google", "microsoft", "spotify")
TEMPLATE = REPO_ROOT / "vault-template"

OK = "  ok   "
WARN = " warn  "
FAIL = " FAIL  "


def _find_claude() -> str | None:
    """Locate a Claude Code CLI the Agent SDK will actually run.

    Native install first, because it is the only stable *executable*. A ``.cmd``
    shim from npm is on PATH and looks fine, but the SDK refuses it: Windows runs
    batch files through cmd.exe, which can execute commands injected via
    arguments, and there is no reliable escaping for cmd.exe. The VSCode
    extension's copy is a real .exe but sits behind a version number that moves
    on every update.
    """
    native = Path.home() / ".local" / "bin" / "claude.exe"
    if native.exists():
        return str(native)

    found = shutil.which("claude")
    if found:
        return found

    ext_root = Path.home() / ".vscode" / "extensions"
    candidates = sorted(ext_root.glob("anthropic.claude-code-*/resources/native-binary/claude.exe"))
    return str(candidates[-1]) if candidates else None


def _claude_problem(path: str) -> str | None:
    """Why the SDK would refuse this CLI, if it would.

    'It exists' is not the same as 'it works'. An earlier version of this check
    passed the npm .cmd shim as healthy, and the bot then failed on its first
    message with a CLIConnectionError.
    """
    lowered = path.lower()
    if lowered.endswith((".cmd", ".bat")):
        return (
            "This is a batch shim, and the Agent SDK refuses to run one: Windows\n"
            "              executes .cmd via cmd.exe, which can run commands injected\n"
            "              through arguments. Install the native build:\n"
            "                irm https://claude.ai/install.ps1 | iex\n"
            "              then set QM_CLAUDE_CLI to the claude.exe it reports."
        )
    if ".vscode" in lowered:
        return (
            "This is the VSCode extension's private copy. Its path contains a\n"
            "              version number and moves on every extension update, which\n"
            "              would break the bot without warning. Install the native build:\n"
            "                irm https://claude.ai/install.ps1 | iex"
        )
    if not Path(path).exists():
        return "That path does not exist."
    return None


def _configure_logging(settings) -> None:
    """Console (stderr) AND a durable file - stdout disappears the moment a job
    is backgrounded or run by Task Scheduler, and stdout is the protocol
    channel for ``qm mcp``, so nothing here may write to it."""
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_path, maxBytes=10_000_000, backupCount=5, encoding="utf-8", delay=True
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
        force=True,
    )
    # httpx logs every request URL at INFO, and Ticketmaster and Klipy take
    # their API key as a query parameter - INFO here writes secrets to disk.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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
        print("              fix: irm https://claude.ai/install.ps1 | iex")
        problems += 1
    else:
        problem = _claude_problem(claude)
        if problem:
            print(f"[{FAIL}] claude cli  {claude}")
            print(f"              {problem}")
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
        ("NOTION_TOKEN", settings.notion_token, "notion mirror"),
        ("DISCORD_BOT_TOKEN", settings.discord_bot_token, "the bot"),
        ("DISCORD_OWNER_ID", settings.discord_owner_id, "who the bot answers"),
        ("GOOGLE_CLIENT_ID", settings.google_client_id, "calendar + gmail"),
        ("GOOGLE_CLIENT_SECRET", settings.google_client_secret, "calendar + gmail"),
        ("MS_CLIENT_ID", settings.microsoft_client_id, "to do"),
        ("SPOTIFY_CLIENT_ID", settings.spotify_client_id, "taste signal"),
        ("SPOTIFY_CLIENT_SECRET", settings.spotify_client_secret, "taste signal"),
        ("TICKETMASTER_API_KEY", settings.ticketmaster_api_key, "events"),
        ("LASTFM_API_KEY", settings.lastfm_api_key, "taste signal"),
        ("LASTFM_USER", settings.lastfm_user, "taste signal"),
        ("KLIPY_API_KEY", settings.klipy_api_key, "optional - gif search"),
    ]
    print()
    for name, value, why in secrets:
        mark = OK if value else WARN
        state = "set" if value else f"not set ({why})"
        print(f"[{mark}] {name:<22} {state}")

    for key, why in (("wishlist.page_id", "wishlist price checks"), ("notion.claude_page_id", "agent's Notion page")):
        section, name = key.split(".")
        value = (settings.prefs.get(section) or {}).get(name) or ""
        print(f"[{OK if value else WARN}] {key:<22} {value or f'not set in config.toml ({why})'}")

    for service in INTEGRATIONS:
        mod = importlib.import_module(f".integrations.{service}", __package__)
        labels = mod.accounts(settings)
        if service == "google":
            labels = [f"{lb} ({'+'.join(mod.account_services(settings, lb)) or 'no access'})" for lb in labels]
        mark = OK if labels else WARN
        print(f"[{mark}] {service} accts  {', '.join(labels) or f'none - run: qm auth {service} <label>'}")

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
            "90-System/state.db\n90-System/state.db-wal\n90-System/state.db-shm\n90-System/turn.lock\n",
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
    (log.warning if stats.held_back else log.info)("notion sync: %s", stats.summary())
    for title, err in stats.failed:
        log.warning("notion sync failed for %s: %s", title, err)

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


def _only_instance(settings, name: str):
    """This process's hold on ``<name>.lock``, or None (and says so) if another
    ``qm <name>`` already runs. Two connected bots double-reply."""
    from . import procs

    lock = procs.instance_lock(settings.log_path.parent, name)
    if lock is None:
        log.warning("another qm %s is already running; not starting a second", name)
        print(f"qm {name} is already running (qm restart restarts it, qm quit stops it).")
    return lock


def cmd_bot(args: argparse.Namespace) -> int:
    from .surfaces.discord_bot import run

    settings = load_settings()
    if not (lock := _only_instance(settings, "bot")):
        return 1
    try:
        return run(settings)
    finally:
        lock.release()


def cmd_web(args: argparse.Namespace) -> int:
    from .surfaces.web import serve

    settings = load_settings()
    if not (lock := _only_instance(settings, "web")):
        return 1
    try:
        print(f"Dashboard at http://{args.host}:{args.port}/  (Ctrl+C to stop)")
        return serve(settings, host=args.host, port=args.port)
    finally:
        lock.release()


def cmd_serve(args: argparse.Namespace) -> int:
    from . import procs

    settings = load_settings()
    if not (lock := _only_instance(settings, "serve")):
        return 1
    log.info("supervisor starting bot and web")
    try:
        procs.Supervisor(procs.RESTARTED, settings.log_path.parent).run()
    finally:
        lock.release()
    return 0


def _run_job(name: str, job, dry_run: bool, sent: str) -> int:
    """Shared by the scheduled jobs: never die silently, always say what happened."""
    try:
        text = job(load_settings(), dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - an unattended job must report, not vanish
        log.exception("%s failed", name)
        print(f"{name} failed: {exc}")
        return 1
    if dry_run:
        print(text or "(nothing to report)")
    else:
        print(sent if text.strip() else "Nothing to report - nothing sent.")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    from . import digest

    return _run_job("digest", digest.run_digest, args.dry_run, "Digest sent and archived.")


def cmd_tidy(args: argparse.Namespace) -> int:
    from . import claude_tidy

    settings = load_settings()
    try:
        print(claude_tidy.run_tidy(settings, dry_run=args.dry_run))
    except Exception as exc:  # noqa: BLE001 - unattended: report, don't vanish
        log.exception("claude page tidy failed")
        print(f"Tidy failed: {exc}")
        return 1
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    from . import reconcile

    settings = load_settings()
    try:
        print(reconcile.run_reconcile(settings, dry_run=args.dry_run))
    except Exception as exc:  # noqa: BLE001 - unattended: report, don't vanish
        log.exception("reconcile failed")
        print(f"Reconcile failed: {exc}")
        return 1
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    from . import vault_push

    settings = load_settings()
    try:
        print(vault_push.run_push(settings.vault))
    except Exception as exc:  # noqa: BLE001 - unattended: report, don't vanish
        log.exception("vault push failed")
        print(f"Vault push failed: {exc}")
        return 1
    return 0


def cmd_minecraft(args: argparse.Namespace) -> int:
    from .integrations import minecraft

    settings = load_settings()
    try:
        if args.action == "setup":
            print(minecraft.setup(settings, accept_eula=args.accept_eula))
        elif args.action == "start":
            print(minecraft.start(settings))
        elif args.action == "stop":
            print(minecraft.stop(settings))
        elif args.action == "cmd":
            print(minecraft.command(settings, " ".join(args.text)))
        elif args.action == "op":
            print(minecraft.grant_op(settings, " ".join(args.text)))
        else:
            print(minecraft.status(settings))
            print(f"Folder: {minecraft.server_dir(settings)}")
    except minecraft.MinecraftError as exc:
        print(exc)
        return 1
    return 0


def cmd_quit(args: argparse.Namespace) -> int:
    from . import procs

    stopped = procs.quit_all()
    print("Stopped: " + ", ".join(stopped) if stopped else "Nothing was running.")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    from . import procs

    out_dir = load_settings().log_path.parent
    stopped, started, failed = procs.restart(out_dir)
    print("Stopped: " + (", ".join(stopped) or "nothing was running"))
    if started:
        print("Started: " + ", ".join(started) + f"  (console output in {out_dir})")
    for line in failed:
        print("FAILED: " + line)
    return 1 if failed else 0


def cmd_queue(args: argparse.Namespace) -> int:
    from . import dev_queue

    path = dev_queue.queue_path(load_settings())
    if args.text:
        dev_queue.add(path, " ".join(args.text))
        print("Queued.")
    print(dev_queue.listing(path))
    print(f"\nFile: {path}")
    return 0


def cmd_presale(args: argparse.Namespace) -> int:
    from . import digest

    return _run_job("presale check", digest.run_presale_check, args.dry_run, "Presale ping sent.")


def cmd_schedule(args: argparse.Namespace) -> int:
    from . import schedule

    if args.action == "install":
        settings = load_settings()
        for line in schedule.install(settings, digest_cadence=args.digest_cadence):
            print(f"Scheduled: {line}")
        print(
            f"\nBot + web: started at logon and kept running ({schedule.SERVICE_TASK}; qm restart starts it now). "
            f"Notion sync: daily at {schedule.SYNC_HOUR:02d}:00. Digest: {args.digest_cadence}. "
            f"Presale check: daily at {schedule.PRESALE_HOUR:02d}:00. "
            f"Reconcile: daily at {schedule.RECONCILE_TIME}. Vault push: daily at {schedule.PUSH_TIME}."
        )
    elif args.action == "remove":
        for line in schedule.remove():
            print(f"Removed: {line}")
    else:
        print(schedule.status())
    return 0


def cmd_mute(args: argparse.Namespace) -> int:
    settings = load_settings()
    added = mutes.add(settings.muted_file, args.item_id, args.summary or "", args.reason or "")
    if added:
        print(f"Muted {args.item_id}. It won't be raised again.")
    else:
        print(f"{args.item_id} was already muted.")
    return 0


def _register_mcp_servers(settings) -> Path:
    """Point the vault's .mcp.json at our servers so terminal `claude` gets the
    same integrations as the bot. Merges; never drops servers added by hand."""
    import json

    from .agent import integration_servers

    path = settings.vault / ".mcp.json"
    config = json.loads(path.read_text("utf-8")) if path.exists() else {}
    config.setdefault("mcpServers", {}).update(integration_servers())
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def cmd_auth(args: argparse.Namespace) -> int:
    settings = load_settings()
    mod = importlib.import_module(f".integrations.{args.service}", __package__)
    print(f"Opening a browser to authorise {args.service} account '{args.label}'...")
    try:
        # Every integration's own error class is a RuntimeError, as is a
        # missing client id from settings.require().
        who = mod.authorize(settings, args.label, args.only) if args.service == "google" else mod.authorize(settings, args.label)
    except RuntimeError as exc:
        print(f"Failed: {exc}")
        return 1
    print(f"Authorised '{args.label}' as {who}.")
    print(f"Token saved to {mod.token_path(settings, args.label)}")
    print(f"Registered MCP servers in {_register_mcp_servers(settings)}")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    # stdout is the protocol channel from here on - nothing else may print.
    importlib.import_module(f".servers.{args.server}", __package__).server.run("stdio")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252, which can't encode most of what a
    # model writes (em dashes, curly quotes, ...). Without this, `qm digest`
    # crashes on its own output the first time the prose isn't pure ASCII.
    # Under pythonw (the logon task) there is no console: both are None.
    windowless = sys.stdout is None or sys.stderr is None
    if windowless:
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout, sys.stderr = sys.stdout or devnull, sys.stderr or devnull
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(prog="qm", description="Quartermaster")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check configuration and dependencies").set_defaults(
        func=cmd_doctor
    )

    p_init = sub.add_parser("init", help="create the vault from the template")
    p_init.add_argument("--force", action="store_true", help="overwrite existing template files")
    p_init.set_defaults(func=cmd_init)

    p_sync = sub.add_parser("sync", help="pull Notion into the vault")
    p_sync.add_argument("--force", action="store_true",
                        help="refetch every page, and remove orphans even past the mass-deletion brake")
    p_sync.set_defaults(func=cmd_sync)

    sub.add_parser("bot", help="run the Discord bot").set_defaults(func=cmd_bot)
    sub.add_parser("serve", help="run bot + web, restarting them if they exit (the logon task runs this)").set_defaults(
        func=cmd_serve)

    p_web = sub.add_parser("web", help="local dashboard: bot status, jobs, turns, live log, guide")
    p_web.add_argument("--host", default="127.0.0.1", help="non-localhost requires QM_WEB_TOKEN")
    p_web.add_argument("--port", type=int, default=8766)
    p_web.set_defaults(func=cmd_web)

    p_digest = sub.add_parser("digest", help="build and send the weekly digest")
    p_digest.add_argument(
        "--dry-run", action="store_true", help="print what would be sent; don't send, archive, or record it"
    )
    p_digest.set_defaults(func=cmd_digest)

    p_presale = sub.add_parser("presale-check", help="check for presales opening today")
    p_presale.add_argument(
        "--dry-run", action="store_true", help="print what would be sent; don't send or record it"
    )
    p_presale.set_defaults(func=cmd_presale)

    p_tidy = sub.add_parser("tidy", help="propose a cleaned-up Claude page (you confirm in Discord)")
    p_tidy.add_argument("--dry-run", action="store_true", help="print the rewrite; propose nothing")
    p_tidy.set_defaults(func=cmd_tidy)

    p_reconcile = sub.add_parser("reconcile", help="dedupe and tidy facts/lessons; DM any conflicts found")
    p_reconcile.add_argument("--dry-run", action="store_true", help="print what it would change; write nothing")
    p_reconcile.set_defaults(func=cmd_reconcile)

    sub.add_parser("push", help="commit everything in the vault and push it to its remote").set_defaults(
        func=cmd_push
    )

    p_mc = sub.add_parser("minecraft", help="the Paper server friends reach over Tailscale")
    p_mc.add_argument("action", nargs="?", default="status", choices=["status", "setup", "start", "stop", "cmd", "op"])
    p_mc.add_argument("text", nargs="*", help="cmd: the server command (whitelist add Steve); op: a player to whitelist and op")
    p_mc.add_argument("--accept-eula", action="store_true", help="setup: you agree to Mojang's EULA")
    p_mc.set_defaults(func=cmd_minecraft)

    sub.add_parser("quit", aliases=["stop"], help="stop every running Quartermaster process (bot, web, jobs)").set_defaults(
        func=cmd_quit
    )
    sub.add_parser("restart", help="restart the bot and dashboard in the background").set_defaults(
        func=cmd_restart
    )

    p_queue = sub.add_parser("queue", help="list (or add to) the dev queue of code changes")
    p_queue.add_argument("text", nargs="*", help="what to change; omit to list the queue")
    p_queue.set_defaults(func=cmd_queue)

    p_schedule = sub.add_parser("schedule", help="manage the Windows Task Scheduler entries")
    p_schedule.add_argument("action", choices=["install", "remove", "status"])
    p_schedule.add_argument(
        "--digest-cadence", choices=["daily", "weekly"], default="weekly",
        help="how often the digest task fires (default weekly; use daily as a proof-of-concept run)",
    )
    p_schedule.set_defaults(func=cmd_schedule)

    p_mute = sub.add_parser("mute", help="permanently silence an item")
    p_mute.add_argument("item_id", help="e.g. stale:abc123 or event:artist/Tool")
    p_mute.add_argument("--summary", help="human-readable label")
    p_mute.add_argument("--reason", help="why, for future reference")
    p_mute.set_defaults(func=cmd_mute)

    p_auth = sub.add_parser("auth", help="authorise an integration account")
    p_auth.add_argument("service", choices=INTEGRATIONS)
    p_auth.add_argument("label", help="your name for this account, e.g. personal or school")
    p_auth.add_argument(
        "--only", nargs="+", choices=["calendar", "gmail"],
        help="google only: request just these services (default: both; you can also untick one in the browser)",
    )
    p_auth.set_defaults(func=cmd_auth)

    p_mcp = sub.add_parser("mcp", help="run an MCP server over stdio (started by Claude, not you)")
    p_mcp.add_argument("server", choices=MCP_SERVERS)
    p_mcp.set_defaults(func=cmd_mcp)

    args = parser.parse_args(argv)
    if windowless and args.command != "serve":
        return _rerun_in_hidden_console(argv if argv is not None else sys.argv[1:])
    try:
        _configure_logging(load_settings())
    except RuntimeError:
        pass  # no vault configured yet; doctor says so, and there is nowhere to log
    return int(args.func(args) or 0)


def _rerun_in_hidden_console(argv: list[str]) -> int:
    """A scheduled job starts under pythonw so no window opens, but it runs
    console programs (claude.exe, git, schtasks), and a console program with no
    console to inherit opens a window of its own. So run the job once more as
    python.exe with CREATE_NO_WINDOW: a hidden console that everything below it
    shares. (Registered as qm.exe, every job opened a Windows Terminal.)"""
    python = Path(sys.executable).with_name("python.exe")
    return subprocess.run(
        [str(python), "-m", "quartermaster.cli", *argv],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
