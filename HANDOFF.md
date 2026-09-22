# Quartermaster — handoff

Written for: the next agent or developer picking this up cold. This file covers
what exists and why; what's planned is in `docs/ROADMAP.md`.

State as of 2026-09-22: Phases 1–4 done, Phase 5 (infra) next. 207 tests.

## What this is

A personal agent sharing one markdown vault and one Claude subscription:

1. **A weekly digest** — Discord DM: calendar, events worth travelling to,
   wishlist price drops, stale Notion pages. Running **daily** as a proof of
   concept; `qm schedule install --digest-cadence weekly` switches to Sundays.
2. **An assistant** — Discord DMs and Claude Code, sharing one conversation.
3. **A Discord bot** — natural-language moderation and expression.

Code is this **public** repo. The vault is a **separate private** repo at
`QM_VAULT_PATH`. Personal data never goes in this repo; a pre-commit hook
(`git config core.hooksPath .githooks`) blocks credentials and PII — fix the
content, don't `--no-verify` past it.

## Three rules that explain most decisions

1. **One source of truth per thing.** Notion is human-authored; the vault mirrors
   it one-way and adds what the agent learns. No sync conflicts exist anywhere.
2. **Mute is permanent, reminding is the default.** Everything nudge-able has a
   stable id and surfaces until muted in `90-System/muted.md` (hand-editable).
3. **Markdown for what a human reads, SQLite for what the machine counts.**
   `state.db` never holds the only copy of anything; a full sync rebuilds it.

## Layout

```
src/quartermaster/
├── cli.py            qm doctor/init/sync/bot/digest/presale-check/schedule/mute/auth/mcp
├── config.py         secrets from .env, preferences from the vault's 90-System/config.toml
├── agent.py          Agent SDK wrapper; Profile + check_tool are the security boundary
├── db.py             state.db — machine state only, rebuildable
├── mutes.py          the mute list
├── notion_sync.py    one-way Notion pull;  notion_clean.py strips Notion's XML/expiring URLs
├── digest.py         digest + presale orchestration;  stale.py stale-page heuristic
├── schedule.py       Windows Task Scheduler wiring (schtasks.exe)
├── discord_ops.py    OpsPlan, permissions, hierarchy, matching (pure, well-tested)
├── integrations/     google, microsoft, spotify (OAuth; shared accounts.py),
│                     notion (REST), ticketmaster, prices
├── servers/          MCP servers over stdio (`qm mcp <name>`); shared helpers in __init__
└── surfaces/         discord_bot (routing), moderation (preview/confirm/execute),
                      digest_send (one-shot DM for scheduled jobs)
vault-template/       copied into a new vault by `qm init`
```

## Running it

```bash
./.venv/Scripts/qm.exe doctor              # first thing when anything misbehaves
./.venv/Scripts/qm.exe bot                 # run the bot (stop the old one first!)
./.venv/Scripts/qm.exe digest --dry-run    # preview the digest; also presale-check --dry-run
./.venv/Scripts/qm.exe schedule install --digest-cadence daily   # or weekly
./.venv/Scripts/python.exe -m pytest tests/ -q
```

**Stop the bot before restarting it** (`Get-Process qm | Stop-Process -Force`):
there's no service wrapper yet, and two connected instances double-reply.

**Scheduled tasks** (logged-in only): Notion sync 07:00 daily, presale check
08:00 daily, digest at `digest.hour` daily or weekly. Re-run `qm schedule
install` after changing `schedule.py` — the sync task only exists once it has
been re-installed.

**Logging.** `cli.main` configures it once for every command: console (stderr)
plus `%LOCALAPPDATA%\quartermaster\Logs\quartermaster.log` (10MB x5). Every agent
turn (model, prompt, each tool call/result truncated, cost — grep the
`[turn_id]`), every denied tool, every moderation decision, every sync failure
and every MCP tool failure lands there. Prompts and tool results are logged
truncated, so email snippets and DMs do appear in it. `httpx` is held at WARNING
because it logs request URLs, and Ticketmaster/Klipy keys are query parameters.
Use `logging`, never `print`, for diagnostics — `print` is block-buffered to a
pipe and vanished once already.

## Security model

### Profiles (`agent.py`)

| | owner (DMs) | public (channels) | parser | digest |
| --- | --- | --- | --- | --- |
| `cwd` | vault | outside it | outside it | vault |
| `tools` | vault + research + Skill | `[]` | `[]` | `[]` |
| MCP | google, microsoft, spotify | none | none | none |
| session | shared with CLI | separate | none | none |
| enabled | yes | **no** | yes | yes |

Containment is enforced three ways, because each alone has leaked:

1. **`tools`** decides what exists. `allowed_tools` only pre-approves — a profile
   with `allowed_tools=[]` once still had the whole toolset
   (`test_public_profile_is_given_no_builtin_tools_at_all`).
2. **`permission_mode="dontAsk"`** refuses anything not pre-approved. This matters
   because the CLI also loads the account's **claude.ai connectors** (Gmail send,
   Drive share, Notion edit) into every profile.
3. **`check_tool`**, a `PreToolUse` hook, re-checks the allow-list and confines
   Read/Write/Edit/Glob/Grep to `profile.cwd`. Writes to `.claude/`, `.mcp.json`
   and `.git/` are refused even inside the vault — each is code execution on a
   later run. Live-verified 2026-09-22.

`Bash` and `NotebookEdit` are withheld from every chat profile. Don't pass the
SDK `skills="all"`: it pre-approves `Skill` for every profile.

Known residual risk: the owner holds `WebFetch`, and email/web pages are
attacker-written. Email bodies are wrapped in untrusted-content markers; that
lowers the exfiltration risk, doesn't remove it.

### Moderation: the model parses, code executes

`discord_ops.py` + `surfaces/moderation.py`. The model never holds a moderation
tool: it emits a structured `OpsPlan`; code checks the invoker's real Discord
permission in that channel, role hierarchy both ways, gathers matches, confirms
(destructive only), executes. The model is never consulted after parsing, so
channel text can at worst produce a plan a human declines.

Deliberate — don't "fix":
- Limits clamp at 200; a ban keeps message history unless asked; an ambiguous
  name resolves to nothing; the preview is built from plan fields, never prose;
  confirmation timeout = decline.
- The matcher skips the bot's own messages and ones mentioning it (else it
  deletes its own preview).
- `react`/`say`/`gif`/`count` skip confirmation.
- The bot pings nobody by default (`AllowedMentions.none()` on the client);
  `say` may ping users but never @everyone or roles — the bot's permission must
  not stand in for the invoker's.
- `unban` resolves names against the ban list, not members.

## Session sharing

The owner profile runs with `cwd` = the vault, like `claude` in a terminal, so
both write to the same `~/.claude/projects/<encoded-vault>/`, and
`continue_conversation=True` picks up the other surface's last turn. That's why
`agent.ask()` calls `query()` per turn instead of holding a `ClaudeSDKClient`.
Sync is turn-level, not live. Verified with one session file holding both.

## The digest (Phase 4)

`digest.build_payload` runs independent collectors — calendar (Google), events
(Ticketmaster, bucketed into distance bands by true haversine distance, capped at
`MAX_EVENTS_PER_BAND`=60 because an uncapped run once built a 1.15MB, $2.23
prompt), wishlist price drops, stale Notion pages — filters everything through
the mute list, and hands one JSON blob to `agent.digest_profile` (no tools,
Sonnet). Collectors do no reasoning; the model only phrases.

- **Each collector catches `Exception`** and degrades to an "unavailable" line:
  googleapiclient, spotipy and httpx errors are not `RuntimeError`s.
- **The wishlist is a plain Notion page, not a database** (`wishlist.page_id`).
  `prices.py` walks its blocks for `rich_text[].href` or `bookmark.url`; unlinked
  items are skipped. Prices come from JSON-LD `Product` markup only; a page
  without it reads "couldn't check", never "no change" (Best Buy and Kinesis
  always do). Downloads are streamed and capped at 2MB, http(s) only.
- **Notion's markdown endpoint drops a bookmark's real URL** (renders an internal
  anchor), which is why `prices.py` reads raw blocks. The general mirror still
  has this gap.
- **Mute ids:** `event:artist/<name>/<id>` (muting `event:artist/Tool` mutes every
  Tool show), `presale:artist/...` (separate namespace on purpose),
  `price:<block id>`, `stale:<full page id>`.
- **`--dry-run`** makes real API calls and records observations (`price_history`,
  `events_seen`) but never increments `surfaced.times_shown`, sends, or archives.
- **The presale check is quiet**: nothing to report sends nothing; a failure is
  logged, never DM'd.
- Repeated dev runs can hit the Pro plan's monthly spend cap; that fails like any
  model error (nothing sent).

## Integrations (Phase 3)

All MCP-exposed via `qm mcp <name>`; the owner profile passes them to the SDK and
`qm auth <service> <label>` writes them into the vault's `.mcp.json` for terminal
`claude`. Tokens: `%LOCALAPPDATA%\quartermaster\tokens\<service>-<label>.json`,
outside both repos.

- **Google** — Calendar read/write, Gmail **read-only by scope**. Accounts carry
  only the services granted on the consent screen.
- **Microsoft To Do** — MSAL public client, tenant `consumers`, `Tasks.ReadWrite`.
  Azure app: "Mobile and desktop applications", redirect `http://localhost`.
  **Never list `openid`/`profile`/`offline_access` in scopes** — MSAL adds them
  and raises `ValueError` if told twice.
- **Spotify** — read-only (`user-top-read user-library-read`). Redirect URI must
  match exactly: `http://127.0.0.1:8765/callback`.

## Environment gotchas (each cost real time)

- **The SDK refuses `.cmd` shims on Windows.** Use the native `claude.exe`
  (`QM_CLAUDE_CLI`); `qm doctor` rejects the npm shim and the VSCode copy.
- **Auth is a Pro subscription**, so failure mode is rate limiting / spend cap,
  not a bill.
- **Python 3.14**, venv at `.venv`. Windows consoles are cp1252: `cli.main`
  reconfigures stdout/stderr to UTF-8.
- **Two privileged Discord intents**: Message Content and Server Members. Without
  Members, the bot runs and moderation turns itself off, saying so.
- **Don't `return` inside `async for message in query(...)`** — drain the loop.
- **The SDK never times out.** `Profile.timeout_seconds` wraps each turn in
  `asyncio.wait_for`; check it first if a surface hangs.
- **Model choice is a per-turn heuristic** (`agent.pick_model`): parser/public →
  Haiku, digest → Sonnet, owner → Sonnet, Opus for long/hard, Haiku for quick
  lookups. Prefix `opus:`/`sonnet:`/`haiku:` to force. `_FALLBACK` steps one tier
  toward Sonnet on a 529.
- Azure Portal with a personal account can loop on `AADSTS50058` if Edge's
  tracking prevention is above Basic.

## Known gaps

- `facts/interests.md` isn't seeded (manual step for now).
- Voice playback needs `PyNaCl` (not installed); voice *moderation* works.
- Voice-mute durations live in memory — a restart leaves the person muted.
- GIF search needs `KLIPY_API_KEY` (Tenor's API shut down 2026-06-30).
- `qm init --force` would overwrite the vault's personalised CLAUDE.md.
- The agent has no Notion write path at all (the original plan's writable
  `claude` page was never built); changes are proposed in `pending.md`.

## Working agreements

- Don't commit unless asked. Personal data goes in the vault repo only.
- **Single-select questions only** — multiSelect dialogs have an unpressable
  submit button in the owner's UI.
- The owner prefers short replies, no preamble, and being told plainly when
  something is broken or an earlier claim was wrong.
