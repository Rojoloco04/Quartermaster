"""``qm web``: a local dashboard for seeing what Quartermaster is doing.

Bot status, scheduled jobs, recent agent turns, the live shared conversation
(Discord and terminal), a tailing log, digests, mutes, the vault drawn as a graph
(/brain), the user guide, and /settings: every preference, the agent's
instructions, facts, lessons, mutes and the dev queue, viewable and editable.
And /chat: the owner's conversation, the same session as the Discord DMs, and
/servers: each game server's status, start/stop and live console.
Those edits, chat turns and start/stops are the only things here that change state.

The log and transcripts hold email snippets and DMs, so it binds to localhost.
Binding anywhere else (a Tailscale address, say) requires ``QM_WEB_TOKEN``;
open ``/?token=...`` once and a cookie carries it after that. Requests must
name an expected Host (a DNS-rebinding page can't read or write through the
owner's browser), and a save must carry the per-run CSRF token from the page.
"""

from .app import build_app, LOCAL_HOSTS, serve
from .chat_view import CHAT_CSS, CHAT_JS, chat_page
from .dashboard_view import (
    bot_status,
    dashboard,
    heartbeat_path,
    HEARTBEAT_STALE,
    read_log_from,
    recent_turns,
    service_line,
    session_entries,
    _STOPPED,
    TAIL_BYTES,
    _TOOL,
    _TURN_DONE,
    _TURN_START,
)
from .layout import CSS, _e, _LINK, _link, LOG_JS, markdown_to_html, NAV, page
from .servers_view import SERVERS_CSS, SERVERS_JS, servers_page
from .settings_view import (
    EDIT_CSS,
    EDIT_JS,
    EDITABLE,
    editable_path,
    EditRefused,
    effective_prefs,
    _FACT,
    FACT_NOTES,
    _fact_title,
    file_block,
    file_hash,
    _parse_pref,
    PREF_CSS,
    PREF_JS,
    render_file,
    save_file,
    secret_status,
    set_pref,
    settings_page,
    _toml_value,
)


