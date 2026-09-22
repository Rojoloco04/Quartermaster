# Quartermaster

A personal agent: a weekly digest, a Notion-mirrored memory, and one
conversation you can reach from Discord or from Claude Code.

`HANDOFF.md` explains what exists and why; `docs/ROADMAP.md` covers what's next.

## Three rules

1. **One source of truth per thing.** Notion is human-authored — knowledge,
   todos, wishlists. The vault mirrors it and adds what the agent learns.
   Nothing is authored twice, so there is no sync conflict to resolve.
2. **Mute is permanent, reminding is the default.** Everything nudge-able has an
   ID. It keeps surfacing until you say stop; then it never comes back.
3. **Markdown for what you read, SQLite for what the machine counts.** The
   database never holds the only copy of anything you'd miss.

## Profiles

The bot can sit in a shared server, so who is talking decides which *profile*
answers — and the profile, not a prompt rule, decides what can be reached.
A prompt instruction is not a boundary against someone who can send arbitrary
text. Different working directories, tool lists and a path-checking hook are.
Details in `HANDOFF.md`.

The **vault lives outside this repo** (set via `QM_VAULT_PATH`) and is its own
private git repo. Code and personal data version separately.

## Setup

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[bot,integrations,dev]"
cp .env.example .env          # then fill it in
./.venv/Scripts/qm.exe doctor # tells you what's still missing
./.venv/Scripts/qm.exe init   # create the vault
./.venv/Scripts/qm.exe sync   # pull Notion
git config core.hooksPath .githooks
```

The hook scans staged content for credentials and personal identifiers and
blocks the commit. This repo is public and git history is permanent.

## Status

| Phase | State |
| --- | --- |
| 1 — Vault and Notion mirror | Done, synced daily |
| 2 — Discord bot and moderation | Done |
| 3 — Google, Microsoft To Do, Spotify | Done |
| 4 — The weekly digest | Done |
| 5 — Infra (service, backups, Tailscale) | Next |

## Tests

```bash
./.venv/Scripts/python.exe -m pytest tests/ -q
```
