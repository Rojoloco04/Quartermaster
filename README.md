# Quartermaster

A personal agent: a weekly digest, a Notion-mirrored memory, and one
conversation you can reach from Discord or from Claude Code.

See `docs/` for design notes.

## Three rules

1. **One source of truth per thing.** Notion is human-authored — knowledge,
   todos, wishlists. The vault mirrors it and adds what the agent learns.
   Nothing is authored twice, so there is no sync conflict to resolve.
2. **Mute is permanent, reminding is the default.** Everything nudge-able has an
   ID. It keeps surfacing until you say stop; then it never comes back.
3. **Markdown for what you read, SQLite for what the machine counts.** The
   database never holds the only copy of anything you'd miss.

## Layout

```
src/quartermaster/
├── config.py          secrets from .env, preferences from the vault's config.toml
├── db.py              state.db — machine state only, rebuildable
├── mutes.py           the mute list (markdown, hand-editable)
├── notion_sync.py     the daily one-way Notion pull
├── cli.py             qm doctor / init / sync / mute
└── integrations/
    └── notion.py      REST client
vault-template/        copied into the real vault by `qm init`
```

The **vault lives outside this repo** (default `~/Vault`, set via `QM_VAULT_PATH`) and is its
own git repo. Code and personal data version separately.

## Setup

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
cp .env.example .env          # then fill it in
./.venv/Scripts/qm.exe doctor # tells you what's still missing
./.venv/Scripts/qm.exe init   # create the vault
./.venv/Scripts/qm.exe sync   # pull Notion
```

Enable the commit guard once per clone:

```bash
git config core.hooksPath .githooks
```

It scans staged content for credentials and personal identifiers and blocks the
commit. This repo is public and git history is permanent, so the guard exists
rather than relying on anyone remembering. Personal content belongs in the
private vault repo. Override a single commit with `git commit --no-verify`.

`qm doctor` is the first thing to run when anything misbehaves. It checks the
vault, the Claude CLI, your auth, and which secrets are set.

## Status

| Phase | State |
| --- | --- |
| 1 — Vault and Notion mirror | Built; needs `NOTION_TOKEN` to run end to end |
| 2 — Discord bot | Not started |
| 3 — Google, Microsoft To Do, Spotify | Not started |
| 4 — The weekly digest | Not started |
| 5 — Infra (Tailscale, Uptime Kuma, restic) | Not started — needs WSL2 + Docker installed |

## Tests

```bash
./.venv/Scripts/python.exe -m pytest tests/ -q
```

Covers the mute list and the mirror's path/frontmatter logic — the two places a
silent bug would do real damage.
