# Using Quartermaster

How to use it day to day. `CLAUDE.md` covers how it works inside.

## Talking to it

DM the bot on Discord. It's the same conversation as `claude` in a terminal
opened in your vault, so you can start on your phone and pick it up at your desk
with `claude --continue`. The **Chat** page in `qm web`
(http://127.0.0.1:8766/chat) is the same conversation again, in the browser.

- It streams: "let me check" arrives as its own message, and a status line at the
  bottom shows what it's doing ("🔧 Checking your calendar…") until it finishes.
- Say "stop" (or "cancel", "nvm") on its own to cancel whatever it's doing.
- Say "start fresh" (or "new chat") to begin a new conversation. The old one
  stays reachable with `claude --resume` in the vault.
- One reply at a time across Discord and the web chat: while one is answering,
  the other says it's busy. "stop" cancels only a reply started from the same
  place. Notion changes proposed from the web chat are still confirmed in Discord.
- Want Quartermaster itself changed? Just say so ("the digest is too long"). It
  can't edit its own code, so it adds the request to its dev queue, and it adds
  things it notices on its own too, marked "(noticed)". When you have Claude
  usage to spare, open Claude Code in the Quartermaster repo and say "work the
  dev queue" (`qm queue` prints the list). Nothing works the queue on its own.
- It answers with Sonnet. Start a message with `opus:` (hard questions) or
  `haiku:` to use another model for that one message.
- After 5 minutes with nothing said, your next message starts a fresh
  conversation (re-sending a long one costs every turn). It still knows
  everything in `facts/`; it just doesn't remember the last chat word for word.
  Change the minutes with `chat.fresh_after_minutes` on the Settings page (0 =
  never).

What it can reach: your vault (read and write), the web, Google Calendar and
Gmail (Gmail is read-only), Microsoft To Do (including checklist steps), Spotify
(read-only), and Notion.

In Notion it writes to your `Claude` page and its sub-pages freely. For any
other page it proposes the change and you get a DM with Confirm/Cancel; nothing
is written unless you press Confirm, and a page it replaces is backed up into
the vault first. Sunday mornings it proposes a tidied Claude page with stale
entries removed, which you confirm the same way (`qm tidy --dry-run` previews
it). It can't change its own
code, schedules or `.env`, and it will tell you so rather than pretend.

## Muting

Say "stop telling me about X" and it's gone for good. Mutes live in
`90-System/muted.md`, one per line; delete a line to un-mute.

- `artist/Tool`: everything about Tool, digest and presale pings alike.
- `event:artist/Tool`: only the digest's event lines for Tool.
- `stale:<page id>`, `price:<block id>`: one stale page, one wishlist item.

From a terminal: `qm mute artist/Tool --reason "not my thing"`.

## The digest and presale pings

- **Digest**: a DM with your week ahead, a 7-day forecast for home (⚠️ on days
  where the weather meets a plan), events worth travelling to, wishlist
  price drops and Notion pages that look abandoned. Daily for now; weekly on
  Sundays once `qm schedule install --digest-cadence weekly` is run.
- **Presale ping**: 08:00, only for artists in your Spotify top artists or named
  in `facts/interests.md`, at most 10. Nothing to report means no message.
- Tune what counts as interesting in `facts/interests.md`; a line you write by
  hand outranks anything inferred. Preferences (home, distance bands, digest
  hour) are in `90-System/config.toml`.
- Preview without sending: `qm digest --dry-run`, `qm presale-check --dry-run`.

## Moderation (in a server)

@mention the bot with a plain request: "delete the last 20 messages about
genshin", "timeout dave for 10 minutes", "voice mute sam for 30s", "react with 🔥"
(reply to the message), "say gm", "gif of a cat". It only does what you could do
yourself with your own Discord permissions. Anything irreversible shows a
preview with Confirm/Cancel first. Replying to a message and saying "delete that"
targets exactly that message.

## Seeing what it's doing

- `qm web` opens a dashboard at http://127.0.0.1:8766: whether the bot is up,
  scheduled jobs, recent turns with cost, the live conversation, a live log,
  digests and mutes.
- **Settings** (http://127.0.0.1:8766/settings) shows everything Quartermaster
  runs on and lets you change it in place: the preferences in force (yours vs
  default), what it knows (`facts/`, including lessons), the agent's
  instructions, mutes and the dev queue. Secrets show only as set or not set.
  Click Edit, change it, Save (or Ctrl+S). A save is refused if the agent
  changed the file after you opened it, and preferences must be valid TOML.
- **Lessons**: tell the bot it got something wrong, in any words, and it records
  the rule in `facts/lessons.md`. Every later reply and digest follows it. Fix
  or delete a lesson on the Settings page.
- **Brain** (http://127.0.0.1:8766/brain) draws what the vault knows as a graph:
  the Notion mirror and `facts/` only. Digests, the inbox, system files and
  READMEs are working material and stay out until they're filed into facts. Each note
  a dot coloured by section, sized by how connected it is, joined by its links,
  its folder, and titles it mentions (dashed). Recently edited notes pulse;
  notes the agent read or wrote lately get a ring and send sparks along their
  edges. Hover for details, click to read a note (facts have an Edit button), drag to move, scroll to zoom,
  double-click to reset, and type in the box to find one. The URL keeps the
  open note, so a link to `/brain#facts/interests.md` opens it.
- The full log: `%LOCALAPPDATA%\quartermaster\Logs\quartermaster.log`, or live
  in PowerShell: `Get-Content -Wait -Tail 50 $env:LOCALAPPDATA\quartermaster\Logs\quartermaster.log`
- `qm doctor` checks configuration when something seems wrong.

## Running it

- `qm quit` (or `qm stop`) stops everything: the bot, the dashboard and any job
  mid-run. Then `qm bot` starts the bot again: two running bots double-reply.
- `qm restart` stops the bot and dashboard and starts both again in the
  background (no terminal needed; closing the terminal doesn't stop them). A
  job mid-run is left alone. Their console output goes to `bot.out` / `web.out`
  next to the log.
- `qm schedule install --digest-cadence daily` (or `weekly`) registers the Notion
  sync (07:00), knowledge reconcile (07:30), presale check (08:00) and digest.
  `qm schedule status` shows them.
- `qm reconcile` checks what Quartermaster knows against itself and Notion:
  it merges duplicates, drops plans whose date has passed, and DMs you a
  question wherever two sources disagree (also listed on the Settings page).
  `--dry-run` shows what it would do. It runs daily at 07:30 on its own.
- Changed something in Notion and don't want to wait for the 07:00 sync? Ask
  the bot to sync ("sync my notion"), or run `qm sync`. Pages you delete in
  Notion leave the mirror and the brain on the next sync. If a sync would remove
  more than half the mirror at once, it removes nothing and says so (usually a
  sharing change in Notion); `qm sync --force` goes ahead anyway.
- Told the bot something changed ("I already have the tickets")? It updates
  every note that says otherwise right away and tells you what it changed.
- `qm sync` pulls Notion into the vault by hand.
