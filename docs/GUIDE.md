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
  never); it applies from the next message, no restart.

What it can reach: your vault (read and write), the web, Google Calendar (add,
move, rename and delete events; a deleted one sits in Google Calendar's trash
for 30 days) and Gmail (Gmail is read-only), Microsoft To Do (including checklist steps), Spotify
(read-only), and Notion.

In Notion it writes to your `Claude` page and its sub-pages freely. For any
other page it proposes the change and you get a DM with Confirm/Cancel; nothing
is written unless you press Confirm, and a page it replaces is backed up into
the vault first. Ask it to delete a page (any page, including under the Claude
page) and you get the same Confirm/Cancel; on Confirm the page is saved to the
vault and moved, with everything under it, to Notion's trash, where you can
restore it. Sunday mornings it proposes a tidied Claude page with stale
entries removed, which you confirm the same way (`qm tidy --dry-run` previews
it). It can't change its own
code, schedules or `.env`, and it will tell you so rather than pretend.

## Muting

Say "stop telling me about X" and it's gone for good. Mutes live in
`System/muted.md`, one per line; delete a line to un-mute.

- `artist/Tool`: everything about Tool, events and on-sales alike.
- `event:artist/Tool`: only Tool's events; `presale:artist/Tool`: only its on-sales.
- `stale:<page id>`, `price:<block id>`: one stale page, one wishlist item.

From a terminal: `qm mute artist/Tool --reason "not my thing"`.

## The digest

One DM every morning at 08:00, in the same layout every day. It's put together
at 03:15 with the other night jobs (so it never eats into your daytime Claude
usage) and the bot sends it at 08:00, so the calendar is as of 03:15:

- **📅 Calendar**: the next 7 days. All-day entries whose title contains a word
  in `digest.ignore_calendar` (reminders, anniversaries) are left out.
- **🎟️ On sale soon**: public on-sales in the next 30 days for acts in your
  Spotify/Last.fm top artists or named in `facts/interests.md`, with any
  presale, the show's date and a ticket link. Each one is shown once.
- **🎤 Events**: shows in the next 30 days worth knowing about, by distance
  (local, day trip, weekend; the weekend band is music only). Each shows when
  first found, and once more in the week it happens. One show sold as several
  listings (day passes and a bundle) is one line with a link per listing.
- **💸 Wishlist**: price drops; on Sundays, also the links it couldn't read.
- **🗂️ Notion**: pages that look abandoned, at most once a week each.

Code builds every line; the model only picks which new events are worth one,
and writes the few words in italics saying why. Each day's digest is archived
in the vault's `digests/` as markdown and as JSON.

- Tune what counts as interesting in `facts/interests.md`; a line you write by
  hand outranks anything inferred. Preferences (home, distance bands, the 30
  days, digest hour) are in `System/config.toml`.
- Preview without sending: `qm digest --dry-run`.
- See it in Discord without using it up: `qm digest --test` DMs you the real
  digest with a "test run" line on top, and marks nothing as shown, so the
  next real one still has everything. Neither a test nor a dry run changes
  what later digests show.
- `qm digest --reset` makes the digest forget what it has already offered and
  shown (events, on-sales, price drops, stale pages), so the next run looks like
  the first one. Mutes stay. Add `--test` to reset and send a test in one go.
  The next real digest will repeat what earlier ones sent.
- `qm schedule install --digest-cadence weekly` makes it Sundays only.

## Moderation (in a server)

@mention the bot with a plain request: "delete the last 20 messages about
genshin", "timeout dave for 10 minutes", "voice mute sam for 30s", "react with 🔥"
(reply to the message), "say gm", "gif of a cat". It only does what you could do
yourself with your own Discord permissions. Anything irreversible shows a
preview with Confirm/Cancel first. Replying to a message and saying "delete that"
targets exactly that message.

Anything that isn't a request for an action ("what's the best seed?", banter)
just gets a reply. That side of the bot knows nothing about you and can't do
anything, and each person gets `public.replies_per_hour` replies an hour
(default 20), since it runs on your subscription. It reads the channel's last
`public.context_messages` messages (default 25, about 5000 characters at most) before
replying, so "is he spitting fire?" works without explaining who "he" is. Set it
to 0 to turn that off.

## Minecraft

A Paper server on this PC that friends join over Tailscale (no port forwarding).
DM the bot: "start the minecraft server", "who's on?", "whitelist Steve", "set
it to day", "stop the server". It asks before stopping if people are online.
Commands that hand out power (`op`, `execute`, ...) are refused, from chat and
`qm minecraft cmd` alike. To make someone an op (yourself first), with the
server running: `qm minecraft op <name>` in a terminal, which also whitelists them.

- One-time: `qm minecraft setup --accept-eula` downloads the newest stable Paper
  (it tells you if Java is too old), whitelists by default and turns on RCON,
  which is how the bot talks to it. Re-run it to update Paper (server stopped).
- `qm minecraft` / `start` / `stop` / `cmd whitelist add Steve` do the same from a
  terminal. The server lives in `%LOCALAPPDATA%\quartermaster\minecraft`
  (`minecraft.dir`), with `minecraft.memory_gb` of RAM (default 4).
- Friends: they install Tailscale, you share this machine with them from the
  Tailscale admin console, they whitelist-in and connect to this PC's Tailscale
  address (`tailscale ip -4`) on port 25565.
- `qm quit` leaves the server running; stop it with the bot or `qm minecraft stop`.
- The **Servers** tab in `qm web` has a tab per game server: its status, Start
  and Stop buttons, and its live console.

**From your Discord server**, anyone can @mention the bot to ask who's on. Starting,
stopping and commands are for the server's **ops** only, and the bot knows
who's an op by linking Discord accounts to Minecraft names: a friend joins the
game and @mentions "link me to Steve", the bot whispers them a code in-game,
and they @mention "verify <code>". That proves both accounts, so nobody can
claim an op's name. Whoever's on the op list (`ops.json`, via `op` in the game
or by hand) gets control; de-op them and it goes. Stopping asks to confirm.
Links are on the Settings page; delete a line to unlink. You don't need one.

## Satisfactory

A dedicated server on this PC, beside Minecraft, that friends join over
Tailscale. DM the bot: "start satisfactory", "anyone on satisfactory?", "save
the factory", "stop satisfactory" (it saves first, and asks if people are on).
There's no console from chat. Guild channels can't control it yet.

- One-time: `qm satisfactory setup` installs SteamCMD and the server into
  `%LOCALAPPDATA%\quartermaster\satisfactory` (`satisfactory.dir`). Re-run it,
  server stopped, to update.
- Bring an old save across: `qm satisfactory start`, wait a minute, then
  `qm satisfactory import <backup.zip>`. It takes a zip or tarball (or a
  tarball inside a zip, as hosting panels make them), copies the saves and
  blueprints in without overwriting anything, loads the newest save and makes
  its session the one loaded on every start. The old host's
  `ServerSettings.<port>.sav` is left out.
- The server claims itself on first start with a random admin password, kept in
  `qm-server.json` in that folder: use it to log into the server from the
  game's Server Manager (to set a join password, say).
- Saves live where the game puts them: `%LOCALAPPDATA%\FactoryGame\Saved\SaveGames\server`
  (your own single-player saves are the other folders there, untouched).
  `qm satisfactory save` saves now under a new name.
- Friends add the server in the game's Server Manager with this PC's Tailscale
  address (`tailscale ip -4`), port 7777. The firewall allows the server exe
  from the tailnet only (rule "Satisfactory (Tailscale only)", added once from
  an admin PowerShell). If Windows ever asks whether to allow
  `FactoryServer-Win64-Shipping-Cmd.exe`, a dismissed prompt adds a Block rule
  that beats the allow: delete any rule named after the exe.
- `qm quit` leaves it running. Its console is its tab on the Servers page.

## Game server backups

`qm backup` zips each server's world or saves onto the backup drive, one zip
per game: `F:\Backups\servers\<game>\<date>_<time>.zip` (`backups.dir`). It
runs daily at 03:35 on its own. A night where nothing changed writes nothing,
and only the newest 14 zips per game are kept (`backups.keep`). Each tab on
the Servers page says when its last backup was.

- Minecraft: the world, `server.properties`, `ops.json`, the whitelist and ban
  lists. If the server is up, autosave is paused for the few seconds of the
  copy, after everything is flushed to disk.
- Satisfactory: the server's saves and blueprints, `ServerSettings.7777.sav`
  (its claim and admin password) and `qm-server.json`. Your own single-player
  saves aren't included.
- Restoring: stop the server, unzip over its folder (Minecraft: the server
  folder; Satisfactory: `%LOCALAPPDATA%\FactoryGame\Saved\SaveGames`, plus
  `qm-server.json` into the server folder).
- If F: isn't there the job fails, and the dashboard shows it red.

## Seeing what it's doing

- `qm web` opens a dashboard at http://127.0.0.1:8766: whether the bot is up,
  scheduled jobs, recent turns with cost, the live conversation, a live log,
  digests and mutes. It's usually already running (it starts at logon); then
  `qm web` just prints its link.
- **Settings** (http://127.0.0.1:8766/settings) shows everything Quartermaster
  runs on and lets you change it in place: the preferences in force, what it knows (`facts/`, including lessons), the agent's
  instructions, mutes and the dev queue. Secrets show only as set or not set.
  Click Edit, change it, Save (or Ctrl+S). A save is refused if the agent
  changed the file after you opened it, and preferences must be valid TOML.
  Click a preference's value to change it: Enter saves it into `config.toml`
  (comments kept), Esc cancels. Lists (the distance bands) are
  edited in the file.
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

- `qm help` lists every command with a line on what it does and its options;
  `qm help digest` shows one in full.
- The bot and dashboard always run under `qm serve`, a small supervisor that
  starts both and brings either back if it crashes. The `Quartermaster
  Service` task starts it when you log in, with no window. Only one bot can run
  at a time: a second `qm bot` says one is already running and exits (`qm bot`
  by hand is for watching it in a terminal while debugging).
- `qm restart` restarts serve with the bot and dashboard, e.g. after a code
  change, and shows them as one tree: `serve (pid 5) with bot (pid 10), web
  (pid 20)`. A job mid-run is left alone. Console output goes to `bot.out` /
  `web.out` next to the log; the log says when the supervisor restarted one
  and why.
- `qm quit` (or `qm stop`) stops everything: the bot, the dashboard, the
  supervisor and any job mid-run. They stay stopped until `qm restart` or your
  next logon.
- `qm schedule install --digest-cadence daily` (or `weekly`) registers the
  service (at logon) and the night chores, five minutes apart so each finishes
  before the next: Notion sync (03:00), knowledge reconcile (03:05), Claude page
  tidy (Sundays 03:10), digest (03:15), vault push (03:30) and game server
  backup (03:35). The ones that use Claude are done hours before your day
  starts, so they don't count against your daytime usage. Nothing messages you
  at night: the digest, reconcile's questions and tidy's Confirm are held and
  the bot sends them at 08:00 (`digest.hour`). `qm schedule status` shows them.
  They run with no window; their output is in the log (and on the dashboard).
- `qm push` commits everything in the vault and pushes it to its private
  remote; that remote is the vault's backup. It runs daily at 03:30 on its own.
  It never forces: if the push is rejected or a merge is half-done, it fails
  (see the log) and leaves it for you.
- `qm reconcile` checks what Quartermaster knows against itself and Notion:
  it merges duplicates, drops plans whose date has passed, and DMs you a
  question wherever two sources disagree (also listed on the Settings page).
  `--dry-run` shows what it would do. It runs daily at 03:05 on its own (its
  questions arrive at 08:00), or
  ask the bot ("reconcile what you know") and it answers in the conversation.
- Changed something in Notion and don't want to wait for the 03:00 sync? Ask
  the bot to sync ("sync my notion"), or run `qm sync`. Pages you delete in
  Notion leave the mirror and the brain on the next sync. If a sync would remove
  more than half the mirror at once, it removes nothing and says so (usually a
  sharing change in Notion); `qm sync --force` goes ahead anyway.
- Told the bot something changed ("I already have the tickets")? It updates
  every note that says otherwise right away and tells you what it changed.
- `qm sync` pulls Notion into the vault by hand.
