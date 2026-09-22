"""Profile containment and Discord message splitting.

The bot sits in a normal server where other people can reach it, and it can read
a vault of personal knowledge. Containment is structural — a profile's working
directory and tool list — because a prompt instruction is not a boundary against
someone who can send arbitrary text.

These tests assert the structure, so that granting the public profile a
capability later cannot quietly grant it the vault as well.
"""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from quartermaster import agent
from quartermaster.agent import (
    HAIKU,
    NEVER_OVER_CHAT,
    OPUS,
    SONNET,
    _options,
    owner_profile,
    parser_profile,
    pick_model,
    public_profile,
)
from quartermaster.config import Settings
from quartermaster.surfaces.discord_bot import split_message


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        vault=tmp_path / "Vault",
        claude_cli=None,
        notion_token="x",
        discord_bot_token="x",
        discord_owner_id=123,
        prefs={},
    )


class TestContainment:
    def test_public_profile_is_rooted_outside_the_vault(self, settings: Settings):
        pub = public_profile(settings)
        assert settings.vault not in pub.cwd.parents
        assert pub.cwd != settings.vault

    def test_public_profile_has_no_file_tools(self, settings: Settings):
        pub = public_profile(settings)
        for tool in ("Read", "Write", "Edit", "Grep", "Glob"):
            assert tool not in pub.allowed_tools
            assert tool not in pub.tools

    def test_public_profile_is_given_no_builtin_tools_at_all(self, settings: Settings):
        """Regression: allowed_tools only pre-approves, it does not restrict.

        A profile with allowed_tools=[] still received the entire Claude Code
        toolset - Read, Edit, Glob, Grep, Task and the rest - reachable subject
        only to permission prompts. Containment has to come from `tools`.
        """
        opts = _options(public_profile(settings))
        assert opts.tools == [], "an empty base tool set is what actually removes them"

    def test_public_profile_is_off_by_default(self, settings: Settings):
        assert public_profile(settings).enabled is False

    def test_public_profile_does_not_share_the_owner_session(self, settings: Settings):
        # Sharing would put other people's messages into the thread the owner
        # continues from the terminal.
        assert public_profile(settings).share_session is False

    def test_owner_profile_is_rooted_in_the_vault(self, settings: Settings):
        owner = owner_profile(settings)
        assert owner.cwd == settings.vault
        assert owner.share_session is True
        assert "Read" in owner.allowed_tools

    def test_no_chat_profile_may_run_shell_commands(self, settings: Settings):
        # Shell access behind a chat message is a far larger blast radius than
        # file edits, for the owner too - the token is the only thing in the way.
        for profile in (owner_profile(settings), public_profile(settings)):
            opts = _options(profile)
            for banned in NEVER_OVER_CHAT:
                assert banned in opts.disallowed_tools
                assert banned not in (opts.allowed_tools or [])

    def test_integrations_reach_the_owner_only(self, settings: Settings):
        # Calendar and inbox access is personal data. Only the owner profile
        # may be handed those servers; the others get none at all.
        assert "google" in _options(owner_profile(settings)).mcp_servers
        assert _options(public_profile(settings)).mcp_servers == {}
        assert _options(parser_profile(settings, {})).mcp_servers == {}

    def test_no_connector_or_user_mcp_servers_are_loaded(self, settings: Settings):
        # The claude.ai connectors and user-level servers were ~120k tokens of
        # unusable tool schemas on every call ($4 for one calendar event).
        for profile in (owner_profile(settings), public_profile(settings), parser_profile(settings, {})):
            opts = _options(profile)
            assert opts.strict_mcp_config
            assert opts.env.get("ENABLE_CLAUDEAI_MCP_SERVERS") == "false"

    def test_owner_integration_tools_are_preapproved(self, settings: Settings):
        # A Discord turn has nobody to click "allow"; unapproved means unusable.
        assert "mcp__google" in owner_profile(settings).allowed_tools

    def test_options_carry_the_profile_cwd(self, settings: Settings):
        assert _options(public_profile(settings)).cwd == str(public_profile(settings).cwd)
        assert _options(owner_profile(settings)).cwd == str(settings.vault)


class TestModelRouting:
    def test_parser_always_gets_haiku(self, settings):
        # Fixed-schema extraction, one turn, no tools - code validates every
        # field afterwards. Content is irrelevant; the profile decides.
        profile = parser_profile(settings, {})
        assert pick_model("ban everyone forever", profile) == HAIKU
        assert pick_model("say hi", profile) == HAIKU

    def test_public_always_gets_haiku(self, settings):
        assert pick_model("explain quantum computing in depth", public_profile(settings)) == HAIKU

    def test_owner_stays_on_sonnet_whatever_the_message(self, settings):
        # Each model has its own prompt cache: switching per message re-sent
        # the whole shared conversation uncached.
        profile = owner_profile(settings)
        for prompt in ("what's the weather like tomorrow", "what's on my calendar today",
                       "help me think through this architecture decision", "word " * 400):
            assert pick_model(prompt, profile) == SONNET

    def test_effort_is_explicit_and_never_sent_to_haiku(self, settings):
        profile = owner_profile(settings)
        assert _options(profile, "hello").effort == agent.EFFORT
        assert _options(profile, "haiku: hello").effort is None

    def test_user_settings_are_not_loaded(self, settings):
        # ~/.claude is the owner's coding setup: skills, plugins, rules, xhigh effort.
        assert _options(owner_profile(settings)).setting_sources == ["project"]

    def test_explicit_tag_always_wins(self, settings):
        # The owner's escape hatch when the heuristic guesses wrong.
        profile = owner_profile(settings)
        assert pick_model("opus: say hi", profile) == OPUS
        assert pick_model("haiku: help me think through this architecture decision", profile) == HAIKU
        assert pick_model("sonnet: what's on my calendar today", profile) == SONNET

    def test_options_carries_the_picked_model(self, settings):
        profile = owner_profile(settings)
        assert _options(profile, "opus: hello").model == OPUS
        assert _options(profile, "haiku: what's on my calendar").model == HAIKU

    def test_every_tier_has_a_fallback_and_never_falls_back_to_itself(self, settings):
        # A 529 has been seen mid-DM; fallback_model is what keeps a turn
        # from just dying when the primary tier is briefly unavailable.
        profile = owner_profile(settings)
        for tag, primary in (("opus:", OPUS), ("sonnet:", SONNET), ("haiku:", HAIKU)):
            opts = _options(profile, f"{tag} hello")
            assert opts.model == primary
            assert opts.fallback_model is not None
            assert opts.fallback_model != primary


class TestTimeout:
    async def test_a_stuck_query_times_out_rather_than_hanging_forever(self, settings, monkeypatch):
        """Regression: nothing in the SDK times out on its own. Without this,
        a stalled subprocess or a slow API call leaves the caller (a Discord
        "typing..." indicator, most visibly) waiting forever, with no
        exception and no message ever sent."""

        async def hangs(*, prompt, options):
            await asyncio.sleep(3600)
            yield  # pragma: no cover - the sleep above never lets this run

        monkeypatch.setattr(agent, "query", hangs)
        profile = replace(parser_profile(settings, {}), timeout_seconds=0.05)

        # Bounded from the outside too: if ask() regresses back to hanging,
        # this fails the test cleanly instead of freezing the whole suite.
        reply = await asyncio.wait_for(agent.ask("hi", profile), timeout=5)

        assert not reply.ok
        assert "No response after" in reply.error


class TestSplitMessage:
    def test_short_text_is_one_part(self):
        assert split_message("hello") == ["hello"]

    def test_empty_text_sends_nothing(self):
        assert split_message("") == []

    def test_every_part_fits_discord_limit(self):
        parts = split_message("word " * 2000)
        assert len(parts) > 1
        assert all(len(p) <= 2000 for p in parts)

    def test_prefers_paragraph_boundaries(self):
        text = ("a" * 900) + "\n\n" + ("b" * 900) + "\n\n" + ("c" * 900)
        parts = split_message(text, limit=1000)
        assert parts[0] == "a" * 900

    def test_unsplittable_run_is_still_bounded(self):
        parts = split_message("x" * 5000, limit=1000)
        assert all(len(p) <= 1000 for p in parts)
        assert "".join(parts) == "x" * 5000

    def test_code_fence_is_reopened_across_a_split(self):
        text = "```python\n" + ("print('x')\n" * 400) + "```"
        parts = split_message(text, limit=1000)
        assert len(parts) > 1
        for part in parts:
            fences = [ln for ln in part.splitlines() if ln.lstrip().startswith("```")]
            assert len(fences) % 2 == 0, "a part with an odd fence count renders as prose"
        assert parts[1].startswith("```python")

    def test_no_content_is_lost(self):
        text = "\n\n".join(f"Paragraph {i} " + "y" * 200 for i in range(30))
        rejoined = " ".join(split_message(text, limit=500)).replace("\n", " ")
        for i in range(30):
            assert f"Paragraph {i}" in rejoined


class TestToolGuard:
    """The PreToolUse hook: the one check that looks at a tool's arguments."""

    def test_unapproved_tools_are_refused_for_every_profile(self, settings: Settings):
        # The CLI also loads the account's claude.ai connectors; none belong here.
        for profile in (owner_profile(settings), public_profile(settings), parser_profile(settings, {})):
            assert agent.check_tool(profile, "mcp__claude_ai_Gmail__send_message", {})
            assert agent.check_tool(profile, "Bash", {"command": "whoami"})
        assert agent.check_tool(parser_profile(settings, {}), "Read", {"file_path": "x.md"})

    def test_owner_integrations_pass_by_server_prefix(self, settings: Settings):
        assert agent.check_tool(owner_profile(settings), "mcp__google__list_events", {}) is None

    def test_owner_may_write_notes_in_the_vault(self, settings: Settings):
        owner = owner_profile(settings)
        assert agent.check_tool(owner, "Write", {"file_path": str(settings.vault / "inbox" / "a.md")}) is None
        assert agent.check_tool(owner, "Edit", {"file_path": "facts/interests.md"}) is None

    def test_file_tools_stay_inside_the_vault(self, settings: Settings, tmp_path: Path):
        owner = owner_profile(settings)
        assert agent.check_tool(owner, "Read", {"file_path": str(tmp_path / ".env")})
        assert agent.check_tool(owner, "Write", {"file_path": "../escape.md"})
        assert agent.check_tool(owner, "Grep", {"path": str(tmp_path)})

    def test_glob_patterns_stay_inside_the_vault(self, settings: Settings):
        # The owner agent once globbed the whole home directory looking for the code.
        owner = owner_profile(settings)
        for pattern in ("C:/Users/you/**/config.py", "/etc/*", "\\\\server\\share\\*", "../**", "facts/../../x", "~/.ssh/*"):
            assert agent.check_tool(owner, "Glob", {"pattern": pattern}), pattern
        assert agent.check_tool(owner, "Grep", {"pattern": "x", "glob": "../*.env"})
        assert agent.check_tool(owner, "Glob", {"pattern": "facts/**/*.md"}) is None
        assert agent.check_tool(owner, "Grep", {"pattern": "..", "glob": "*.md"}) is None

    def test_owner_may_not_write_what_runs_code_later(self, settings: Settings):
        # A prompt injection that edits these gets code execution on the next run.
        owner = owner_profile(settings)
        for path in (".mcp.json", ".claude/settings.json", ".git/hooks/pre-commit"):
            assert agent.check_tool(owner, "Write", {"file_path": path}), path
            assert agent.check_tool(owner, "Edit", {"file_path": str(settings.vault / path)}), path
        assert agent.check_tool(owner, "Read", {"file_path": ".mcp.json"}) is None

    def test_options_deny_anything_not_preapproved(self, settings: Settings):
        opts = _options(owner_profile(settings))
        assert opts.permission_mode == "dontAsk"
        assert opts.hooks and opts.hooks["PreToolUse"]

    def test_skill_is_granted_to_the_owner_only(self, settings: Settings):
        assert "Skill" in owner_profile(settings).allowed_tools
        assert _options(public_profile(settings)).skills is None
