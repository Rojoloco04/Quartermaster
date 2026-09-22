# Roadmap

Everything planned but not built, with enough design detail to build from. For
what exists and why it looks the way it does, see `CLAUDE.md`.

| Phase | What | State |
| --- | --- | --- |
| 1 | Vault + Notion mirror | Done — synced daily by Task Scheduler |
| 2 | Discord assistant + moderation | Done |
| 3 | Integrations | Done |
| 4 | Weekly digest | Done — running daily as a proof of concept |
| 5 | Infra | **Next** |
| 6–10 | Extensions | Planned |
| — | Added after the original plan | See the end |

Phases 1–4 are described as built in `CLAUDE.md`, including where the build
deviated from the plan (the wishlist is a Notion page, not a database).

Still open from Phase 4:
- **Interest profile** — seed `facts/interests.md` from the Notion mirror,
  Spotify and an import of the owner's Claude.ai chat memories; a hand-written
  line outranks anything inferred. Refine from reactions to recommendations.
- **Switch the digest to weekly** (Sunday) once the daily output looks right.

---

## Phase 5 — Infra

- **Service registration** so the bot survives reboot — and so instances stop
  stacking. Today, restarting by hand can leave two bots connected, which
  double-replies. This is the real fix.
- **Scheduled push** of the vault to its private remote.
- **restic** nightly to a second drive, **with a verified test restore**.
- **Uptime Kuma** watching the bot and the scheduled jobs.
- **Tailscale**; no router port forwarding.
- Needs **WSL2 + Docker**, neither of which is installed yet.

*Done when:* the vault can be destroyed and restored from the remote with nothing
lost.

---

## Extensions — after Phase 5

| Phase | What | Grouped because |
| --- | --- | --- |
| 6 | Notion hygiene, decision log, voice capture | All reuse Phase 1–4 machinery; no new dependencies |
| 7 | HSA and receipt tracking | Real money — its own phase |
| 8 | Travel deals from home | Extends the 500-mile events radius to flights |
| 9 | Lecture transcription + semantic search | Both make the local GPU load-bearing |
| 10 | Jellyfin | Streaming |

### Phase 6

- **Notion hygiene** beyond staleness: duplicate pages, orphans, broken links,
  inconsistent tags.
- **Decision log** — when a decision is made in conversation, record what and why.
  Cheap, and genuinely useful later.
- **Voice capture** — a voice memo sent to Discord is transcribed and filed to
  `inbox/`. The lowest-friction capture path.

### Phase 7 — HSA and receipts

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

### Phase 8 — Travel deals

Flight deals from the home airport. Amadeus Self-Service has a free tier —
**verify it is still available before building**; flight APIs churn.

### Phase 9 — Local GPU work

- **Lecture transcription** via whisper.cpp, output into Notion. The one local-GPU
  use that clearly earns its place.
- **Semantic search** over the vault via Ollama (`nomic-embed-text`) plus
  `sqlite-vec`, fused with FTS5 BM25 by Reciprocal Rank Fusion. Deferred on purpose:
  grep is genuinely enough until the vault reaches a few hundred notes.

**Local generation was evaluated and rejected.** The Agent SDK bills against the
Claude subscription, which removes the cost argument, and an 8GB card caps local
models at 7–9B — a noticeable step down for writing the digest. GPU wear at this
duty cycle is not a real concern. Worth revisiting only if subscription rate limits
start to bite.

### Phase 10 — Jellyfin

Streaming with NVENC hardware transcoding.

---

## Added after the original plan

### Done

- **Discord moderation and expression** — delete, pin, kick, ban, timeout, voice
  mute/deafen/disconnect, react, say, gif. The model parses; code executes. Gated by
  the invoker's own Discord permissions and role hierarchy. See `CLAUDE.md`.

### Planned

- **Jellyfin notifications** — build as a plain webhook that posts to a channel.
  **No agent, no vault, no model cost.** Nothing about it needs Claude, and routing
  it through the agent would only add a path to the vault that has no reason to
  exist.
- **Letting friends talk to the bot.** The public profile exists and is inert
  (`tools=[]`, `enabled=False`, `cwd` outside the vault, no shared session). Turning
  it on means *adding* capabilities to an empty list — never removing access from a
  privileged agent. Note that other people's conversations would spend the owner's
  subscription limits.
- **Voice playback** — needs `PyNaCl` and ffmpeg. The bot currently logs a warning
  that voice is unsupported. Voice *moderation* already works; it is a different API.
- **Restock tracker** for food and snacks: what's running low, with links only.
- **A `/budget` command** in Discord. Links and summaries only: no bank logins,
  nothing that moves money.
- **Game servers for friends**, reachable over Tailscale (after Phase 5). No port
  forwarding.
- **Persistent voice-mute timers.** "Mute for 30s" is scheduled in memory today, so a
  restart leaves the person muted. Move pending undos into `state.db`.

### Considered and rejected

- **An unattended dev runner** working the `!queue` overnight (worktree, shell,
  push, PR). Built, then removed: an agent with a shell and a repo-wide GitHub
  token running unsupervised. The queue is worked in Claude Code with the owner
  present instead. Revisit only with a sandbox and a single-repo token.

- **n8n.** It was the centre of the original homelab plan. The Agent SDK now owns tool
  use and a scheduler owns timing; keeping n8n would mean two things that both "run
  jobs on a timer", with workflows locked in a database instead of versioned.
- **Two-way Notion sync.** Notion is authored by a human; the vault mirrors it one
  way. The only writes back are approved proposals via `pending.md`, plus the one
  page the agent owns. No merge logic, so no conflict bugs.
- **Nightly reflection / auto-distilled memory.** Deferred rather than rejected. A
  folder plus a facts file covers most of the value; add distillation when
  hand-curation becomes a chore.
- **A dedicated Amazon price parser.** No public API, actively blocks scrapers,
  breaks periodically. Generic extraction with an honest "couldn't check" instead.
- **Real-time sync between Discord and the terminal.** Turn-level sync via the
  shared session covers it; live mirroring needs a relay process that can silently
  die.
