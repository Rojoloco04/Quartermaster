# Using Quartermaster

What it does and how to use it. `CLAUDE.md` covers how it works inside.

## Talk to it

DM the bot on Discord, use the **Chat** page in `qm web`, or run `claude` in the
vault: all three are the same conversation. It can read and write your vault,
search the web, manage Google Calendar, read Gmail, read and edit Microsoft To
Do, see your Spotify, and edit Notion. Changes to Notion pages outside its own
`Claude` page wait for your Confirm in Discord.

- "stop" cancels a reply; "start fresh" begins a new conversation.
- "stop telling me about X" mutes it for good (`System/muted.md`).
- Correct it and it remembers the lesson. "Change your tone to X" changes how it talks.
- Ask for a change to Quartermaster itself and it goes on the dev queue.
- Start a message with `opus:` or `haiku:` to switch model for that message.

## The morning digest

A DM at 08:00 with your week's calendar, on-sales and shows for acts you like,
wishlist price drops and Notion pages gone stale. Events within 100 miles are
judged loosely; 100-500 miles only if they match your artists or interests.
Tune it in `facts/interests.md` (a change there gets passed-over shows a second
look) and the bands in `System/config.toml`.
Preview with `qm digest --dry-run`, or `qm digest --test` to get it in Discord.

## In your server

@mention the bot. Plain requests ("delete the last 20 messages", "timeout dave
for 10 minutes", "gif of a cat") work if you have the Discord permission for
them, and anything destructive asks first. Anything else just gets a chat reply.

## Game servers

Minecraft and Satisfactory run on this PC; friends join over Tailscale. DM
"start minecraft", "who's on?", "stop satisfactory", or use the **Servers** page.
Both are backed up to F: every night.

## Settings and status

`qm web` (http://127.0.0.1:8766) shows whether everything is running, the log,
past digests, a graph of what it knows (**Brain**), and **Settings**, where every
preference and note is editable. Pages update themselves every 15 seconds; long
text shows its start with a "more" toggle.

- `qm restart` / `qm quit` restarts or stops the bot and dashboard.
- `qm doctor` checks the setup when something's off.
- `qm help` lists every command.
