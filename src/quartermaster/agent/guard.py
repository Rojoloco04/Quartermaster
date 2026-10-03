"""``check_tool``: the PreToolUse hook that re-checks every tool call against
the profile's allow-list and confines file access to its directory.
"""

from __future__ import annotations

import logging
import re
from pathlib import PureWindowsPath

from .profiles import NEVER_OVER_CHAT, Profile


# Denials are logged under the same name as the turns they interrupt.
log = logging.getLogger("quartermaster.agent")


# File tools and the input key holding the path they touch.
_PATH_KEYS = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "Glob": "path", "Grep": "path"}

# Inside the vault, but writing here is code execution on a later run: hooks
# and MCP servers are launched from .claude/ and .mcp.json, git hooks from .git/.
_PROTECTED = (".claude", ".mcp.json", ".git", ".githooks",
              # Written only through the qm server's queue_change tool, so every
              # entry is one tagged line the owner reviews before acting on it.
              "System/dev-queue.md",
              # Who may control the Minecraft server from Discord. Written only
              # by a link proven in-game (or the owner by hand in /settings):
              # an email must not be able to talk the agent into adding one.
              "System/minecraft-links.md")


def check_tool(profile: Profile, tool: str, tool_input: dict) -> str | None:
    """Why this call is refused, or None if it may run.

    The second line of defence behind ``tools``/``dontAsk``, and the only one
    that looks at arguments: file tools stay inside ``profile.cwd``, and never
    touch the files that would run code later (see ``_PROTECTED``). A prompt
    injection in an email or web page can ask for anything; this is what it
    runs into.
    """
    allowed = tool in profile.allowed_tools or any(
        a.startswith("mcp__") and tool.startswith(a + "__") for a in profile.allowed_tools
    ) or (
        # How the SDK delivers output_schema answers: it only returns data. It
        # was denied once, and the reconcile job looped until max_turns.
        tool == "StructuredOutput" and profile.output_schema is not None
    )
    if not allowed or tool in NEVER_OVER_CHAT:
        return f"{tool} is not available to the {profile.name} profile."

    if tool in ("Glob", "Grep"):
        # A pattern is a path too: Glob("C:/Users/**") once searched the whole
        # home directory with no `path` given.
        pattern = str(tool_input.get("pattern" if tool == "Glob" else "glob") or "")
        if PureWindowsPath(pattern).anchor or pattern.startswith(("/", "\\", "~")) or ".." in re.split(r"[\\/]", pattern):
            return f"{tool} patterns must be relative to {profile.cwd.resolve()}."

    key = _PATH_KEYS.get(tool)
    if key is None or not tool_input.get(key):
        return None  # no path given: the tool defaults to cwd
    root = profile.cwd.resolve()
    target = (root / str(tool_input[key])).resolve()
    if target != root and root not in target.parents:
        return f"{tool} is limited to {root}."
    rel = target.relative_to(root).as_posix()
    if tool not in ("Read", "Glob", "Grep") and any(rel == p or rel.startswith(p + "/") for p in _PROTECTED):
        return f"{tool} may not change {rel} - it controls what runs later, or is the owner's alone to write."
    return None


def _guard(profile: Profile):
    async def hook(input_data: dict, tool_use_id: str | None, context: object) -> dict:
        reason = check_tool(profile, input_data.get("tool_name", ""), input_data.get("tool_input") or {})
        if reason is None:
            return {}
        log.warning("denied %s for %s: %s", input_data.get("tool_name"), profile.name, reason)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return hook
