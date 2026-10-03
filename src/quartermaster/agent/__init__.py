"""The Agent SDK wrapper.

Every surface goes through here, so tool permissions and model choice are
decided in one place rather than scattered across callers.

**Profiles are the security boundary.** The bot lives in a normal Discord server
where other people can eventually talk to it, and it can read a vault full of
personal knowledge. A prompt instruction not to discuss that knowledge is not a
boundary — anyone who can send text can argue with an instruction. So each
profile carries its own working directory and its own tool list, and a profile
without vault tools, rooted outside the vault, physically cannot reach it no
matter what it is asked.

**Session sharing.** The owner profile runs with ``cwd`` set to the vault, the
same as ``claude`` in a terminal, so both write transcripts to
``~/.claude/projects/<encoded-vault-path>/``. With
``continue_conversation=True`` each surface picks up whatever the other said
last: message the bot from your phone, then run ``claude --continue`` in the
vault and the thread is there.

That is also why this does not hold a long-lived ``ClaudeSDKClient``. A
persistent client keeps its own session and would never notice anything said in
the terminal — faster, and it would quietly break the one property that makes
the two surfaces feel like one assistant.

Sync is turn-level, not live. Neither side sees the other mid-turn.
"""

from .guard import check_tool, _guard, _PATH_KEYS, _PROTECTED
from .profiles import (
    CHAT_STYLE,
    digest_profile,
    DISCORD_STYLE,
    integration_servers,
    INTEGRATION_TOOLS,
    MCP_SERVERS,
    NEVER_OVER_CHAT,
    OWNER_LIMITS,
    owner_profile,
    parser_profile,
    Profile,
    public_profile,
    PUBLIC_ROLE,
    reconcile_profile,
    RESEARCH_TOOLS,
    tidy_profile,
    VAULT_TOOLS,
)
from .turn import (
    ask,
    EFFORT,
    _explain,
    _FALLBACK,
    HAIKU,
    _options,
    OPUS,
    _OVERRIDE_TAGS,
    pick_model,
    Progress,
    Reply,
    SONNET,
    transcript_dir,
    _trunc,
)


