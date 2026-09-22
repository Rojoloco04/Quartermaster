# Roadmap

Everything planned but not built, with enough design detail to build from. For
what exists and why it looks the way it does, see `HANDOFF.md`.

| Phase | What | State |
| --- | --- | --- |
| 1 | Vault + Notion mirror | Done — daily schedule not yet wired |
| 2 | Discord assistant + moderation | Done |
| 3 | Integrations | **Next** |
| 4 | Weekly digest | Planned |
| 5 | Infra | Planned |
| 6–10 | Extensions | Planned |
| — | Added after the original plan | See the end |

---

## Phase 3 — Integrations

The groundwork the digest needs. Each exposed as an MCP server so the assistant
can answer on demand, not only in the digest.

- **Google Calendar** — read/write, multiple calendars, OAuth desktop flow.
- **Gmail** — **two inboxes**, read-only. Two token files, one per account.
- **Microsoft To Do** — via Microsoft Graph (`/me/todo/lists`). Needs a free Azure
  app registration, tenant `consumers`.
- **Spotify** — OAuth. Supplies taste signal for the events section.

*Done when:* it answers correctly about the calendar and the To Do list.

---

## Phase 4 — The weekly digest

The flagship. **Sunday evening, Discord DM.** Collectors are plain Python and do no
reasoning — they fetch, normalise, and hand structured data to the Agent SDK, which
writes the prose. Roughly one model call a week.

| Section | Source | Notes |
| --- | --- | --- |
| Week ahead | Google Calendar | Next 7 days |
| Events | Ticketmaster Discovery + Spotify | All categories, filtered against `facts/interests.md` |
| Price drops | Notion wishlist database | Generic structured-data extraction |
| Gone stale | Notion mirror | Untouched 90+ days **and** looks unfinished |

**Deliberately excluded:** deadline reminders, bills, weather. The owner gets those
elsewhere, and a digest that nags gets muted.

### Events

- Ticketmaster Discovery API, free tier (5,000 calls/day, 5 req/s).
- Use `geoPoint` (a geohash — `latlong` is deprecated) plus `radius` centred on the
  home location. Not a maintained list of cities: a radius picks up every metro in
  range for free.
- Query in **three distance bands**. That sets the bar for inclusion *and* keeps each
  query under the API's hard 1,000-result deep-paging cap (`size * page < 1000`),
  which a single 500-mile, one-week query would exceed.

  | Band | Bar | Meaning |
  | --- | --- | --- |
  | 0–60 mi | Low | A weeknight |
  | 60–250 mi | Medium | A day trip |
  | 250–500 mi | High | Worth a weekend |

  Band definitions already live in `vault-template/90-System/config.toml`.
- Spotify covers taste-matching for music; Claude filters everything else against
  the interest profile.

### Presales — a separate same-day ping, not a digest line

A Sunday message is useless for tickets that sold out on Thursday. Uses
`onsaleStartDateTime` from the same collector. Muteable per artist.

### Price checks

- The wishlist is a **Notion database** of product URLs, which the owner already
  maintains. No separate list.
- **Generic structured-data extraction only** — most retailers embed
  machine-readable product data (JSON-LD `Product` / `offers.price`). No
  per-retailer parsers to maintain.
- Sites that yield nothing — Amazon among them — report **"couldn't check"**. Never
  silently "no change". `db.latest_price()` already excludes failed checks so a
  network error can't manufacture a price drop.
- History in `state.db.price_history`.

### Interest profile

Seeded from the Notion mirror, Spotify, and an import of the owner's Claude.ai chat
memories. Written to `facts/interests.md` (hand-editable; a line written by hand
outranks anything inferred), then refined by reactions to recommendations.

### Stale pages

Untouched 90+ days *and* looking unfinished — empty sections, open checkboxes,
stubs. Reference pages written once and never needing edits are not stale.
Threshold in config.

### The mute rule applies to every section

Every item carries a stable ID. It keeps surfacing until the owner says stop, then
never again. `mutes.py` already implements this, including scope nesting — muting
`event:artist/X` silences every show by X.

*Done when:* a digest lands on Sunday and the owner would have missed something
without it.

---

## Phase 5 — Infra

- **Service registration** so the bot survives reboot — and so instances stop
  stacking. Today, restarting by hand can leave two bots connected, which
  double-replies. This is the real fix.
- **Scheduled Notion sync** (daily).
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
  the invoker's own Discord permissions and role hierarchy. See `HANDOFF.md`.

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
- **Persistent voice-mute timers.** "Mute for 30s" is scheduled in memory today, so a
  restart leaves the person muted. Move pending undos into `state.db`.

### Considered and rejected

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
