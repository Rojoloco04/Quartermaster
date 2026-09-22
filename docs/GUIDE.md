# Using Quartermaster

How to use it day to day. `HANDOFF.md` covers how it works inside.

## Talking to it

DM the bot on Discord. It's the same conversation as `claude` in a terminal
opened in your vault, so you can start on your phone and pick it up at your desk
with `claude --continue`.

- It streams: "let me check" arrives as its own message, and a status line at the
  bottom shows what it's doing ("🔧 Checking your calendar…") until it finishes.
- `!stop` cancels whatever it's doing.
- `!new` makes your next message start a fresh conversation. The old one stays
  reachable with `claude --resume` in the vault.
- `!queue <change>` jots down a change you want to Quartermaster itself, word
  for word; `!queue` alone lists them. When you have Claude usage to spare, open
  Claude Code in the Quartermaster repo and say "work the dev queue" (`qm queue`
  prints the list and the file's path). Nothing works the queue on its own.
- Start a message with `opus:`, `sonnet:` or `haiku:` to pick the model for that
  one message. Otherwise it picks: Haiku for quick lookups, Sonnet by default,
  Opus for long or hard questions.

What it can reach: your vault (read and write), the web, Google Calendar and
Gmail (Gmail is read-only), Microsoft To Do (including checklist steps), Spotify
(read-only), and your `Claude` page in Notion. It can't touch the rest of Notion:
it proposes changes in `90-System/pending.md` instead. It can't change its own
code, schedules or `.env`, and it will tell you so rather than pretend.

## Muting

Say "stop telling me about X" and it's gone for good. Mutes live in
`90-System/muted.md`, one per line; delete a line to un-mute.

- `artist/Tool`: everything about Tool, digest and presale pings alike.
- `event:artist/Tool`: only the digest's event lines for Tool.
- `stale:<page id>`, `price:<block id>`: one stale page, one wishlist item.

From a terminal: `qm mute artist/Tool --reason "not my thing"`.

## The digest and presale pings

- **Digest**: a DM with your week ahead, events worth travelling to, wishlist
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
- The full log: `%LOCALAPPDATA%\quartermaster\Logs\quartermaster.log`, or live
  in PowerShell: `Get-Content -Wait -Tail 50 $env:LOCALAPPDATA\quartermaster\Logs\quartermaster.log`
- `qm doctor` checks configuration when something seems wrong.

## Running it

- `qm bot` runs the bot. Stop the old one first
  (`Get-Process qm | Stop-Process -Force`): two running bots double-reply.
- `qm schedule install --digest-cadence daily` (or `weekly`) registers the Notion
  sync (07:00), presale check (08:00) and digest. `qm schedule status` shows them.
- `qm sync` pulls Notion into the vault by hand.
