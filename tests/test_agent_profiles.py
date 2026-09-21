"""Profile containment and Discord message splitting.

The bot sits in a normal server where other people can reach it, and it can read
a vault of personal knowledge. Containment is structural — a profile's working
directory and tool list — because a prompt instruction is not a boundary against
someone who can send arbitrary text.

These tests assert the structure, so that granting the public profile a
capability later cannot quietly grant it the vault as well.
"""

from pathlib import Path

import pytest

from quartermaster.agent import (
    NEVER_OVER_CHAT,
    _options,
    owner_profile,
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
        notion_claude_page_id=None,
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

    def test_options_carry_the_profile_cwd(self, settings: Settings):
        assert _options(public_profile(settings)).cwd == str(public_profile(settings).cwd)
        assert _options(owner_profile(settings)).cwd == str(settings.vault)


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
