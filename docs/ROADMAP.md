# Roadmap

What's planned but not built, with enough design detail to build from, and what
was considered and rejected. What exists is in `CLAUDE.md`; what changed when
is in git.

## Next: Phase 6

- **Notion hygiene** beyond staleness: duplicate pages, orphans, broken links,
  inconsistent tags.
- **Decision log**: when a decision is made in conversation, record what and why.
  Cheap, and genuinely useful later.
- **Voice capture**: a voice memo sent to Discord is transcribed and filed to
  `inbox/`. The lowest-friction capture path.

## The digest

- **More event sources.** Ticketmaster carries few raves and EDM nights, car
  meets or cultural festivals; those sell on Dice, Resident Advisor and
  Eventbrite. The most promising first step is Bandsintown's artist events API:
  every tour date of each top artist, wherever it's ticketed. Verify its terms
  and access before building.
- **Refine the interest profile** from reactions to what the digest shows.
- **Switch to weekly** (Sunday) once the daily output looks right.

## Later phases

| Phase | What | Grouped because |
| --- | --- | --- |
| 7 | HSA and receipt tracking | Real money: its own phase |
| 8 | Travel deals from home | Extends the events radius to flights |
| 9 | Lecture transcription + semantic search | Both make the local GPU load-bearing |
| 10 | Jellyfin | Streaming |

### Phase 7: HSA and receipts

An HSA can reimburse qualified medical expenses **years after the fact**, provided
the documentation was kept. That makes this a ledger with money attached rather
than a filing convenience.

- Photograph a receipt into Discord → parsed into a structured record.
- Receipt images and **one markdown record per expense live in the vault, in git**.
  They are irreplaceable documentation, so they must never exist only in
  `state.db`.
- `state.db` indexes them for totals: amount, date, reimbursed / unreimbursed.
- Digest line: running unreimbursed balance.
- **Hard limit:** it flags what looks likely-qualified and **never asserts tax
  treatment**. What qualifies is IRS Publication 502 territory; it says so rather
  than guessing. `CLAUDE.md` in the vault already states this rule.

### Phase 8: Travel deals

Flight deals from the home airport. Amadeus Self-Service has a free tier:
**verify it is still available before building**; flight APIs churn.

### Phase 9: Local GPU work

- **Lecture transcription** via whisper.cpp, output into Notion. The one local-GPU
  use that clearly earns its place.
- **Semantic search** over the vault via Ollama (`nomic-embed-text`) plus
  `sqlite-vec`, fused with FTS5 BM25 by Reciprocal Rank Fusion. Deferred on purpose:
  grep is genuinely enough until the vault reaches a few hundred notes.

**Local generation was evaluated and rejected.** The Agent SDK bills against the
Claude subscription, which removes the cost argument, and an 8GB card caps local
models at 7–9B, a noticeable step down. GPU wear at this duty cycle is not a real
concern. Worth revisiting only if subscription rate limits start to bite.

### Phase 10: Jellyfin

Streaming with NVENC hardware transcoding.

## Smaller items

- **Jellyfin notifications**: a plain webhook that posts to a channel. **No
  agent, no vault, no model cost.** Routing it through the agent would only add
  a path to the vault that has no reason to exist.
- **More for friends who talk to the bot.** Anything more means *adding*
  capabilities to the public profile's empty tool list, never removing access
  from a privileged agent.
- **Satisfactory from guild channels.** It has no in-game whisper to prove a
  link with, so control would be owner-only or by a Discord role.
- **Game servers on a separate box** instead of this PC.
- **Voice playback**: needs `PyNaCl` and ffmpeg. Voice *moderation* already
  works; it is a different API.
- **Persistent voice-mute timers.** "Mute for 30s" is scheduled in memory, so a
  restart leaves the person muted. Move pending undos into `state.db`.
- **Restock tracker** for food and snacks: what's running low, with links only.
- **A `/budget` command** in Discord. Links and summaries only: no bank logins,
  nothing that moves money.
- **The bot in Docker**, if it moves to a server. Not on this PC: a Linux
  container would split the shared session and need its own Claude login.

## Considered and rejected

- **An unattended dev runner** working the queue overnight (worktree, shell,
  push, PR). Built, then removed: an agent with a shell and a repo-wide GitHub
  token running unsupervised. The queue is worked in Claude Code with the owner
  present instead. Revisit only with a sandbox and a single-repo token.
- **restic** to a second drive. The vault's git remote already keeps every
  version off this machine; what isn't in it is rebuildable (`state.db`) or
  re-creatable (`.env`, OAuth tokens via `qm auth`).
- **Uptime Kuma.** It would run on the PC it watches, so it misses the main
  failure (the PC down); the supervisor already restarts the bot and the
  dashboard shows its heartbeat. If an out-of-band alert is ever wanted, a
  hosted dead-man's switch (healthchecks.io, the bot pinging it) covers the PC
  being off too.
- **n8n.** The Agent SDK owns tool use and a scheduler owns timing; keeping n8n
  would mean two things that both "run jobs on a timer", with workflows locked
  in a database instead of versioned.
- **Two-way Notion sync.** Notion is authored by a human; the vault mirrors it one
  way. The only writes back are proposals the owner confirms in Discord, plus the one
  page the agent owns. No merge logic, so no conflict bugs.
- **Nightly reflection / auto-distilled memory.** Deferred rather than rejected. A
  folder plus a facts file covers most of the value; add distillation when
  hand-curation becomes a chore.
- **A dedicated Amazon price parser.** No public API, actively blocks scrapers,
  breaks periodically. Generic extraction with an honest "couldn't check" instead.
- **Real-time sync between Discord and the terminal.** Turn-level sync via the
  shared session covers it; live mirroring needs a relay process that can silently
  die.
- **Weather in the digest.** The owner checks an app; a text forecast wasn't
  worth the tokens.
