"""Microsoft integration: the parts that don't need a network or a token."""

from pathlib import Path

import pytest

from quartermaster.config import Settings
from quartermaster.integrations import microsoft
from quartermaster.integrations.microsoft import MicrosoftError, due_body, format_task


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    s = Settings(
        vault=tmp_path / "Vault", claude_cli=None, notion_token=None,
        notion_claude_page_id=None, discord_bot_token=None, discord_owner_id=None,
    )
    monkeypatch.setattr(Settings, "tokens_dir", property(lambda self: tmp_path / "tokens"))
    return s


class TestScopes:
    def test_no_reserved_scope_is_requested_explicitly(self):
        """Regression: MSAL adds openid, profile and offline_access to every
        token request itself, and acquire_token_interactive/acquire_token_silent
        raise ValueError if you also pass them. Cost a live auth attempt to find -
        the refresh token still shows up without asking for offline_access by name."""
        reserved = {"openid", "profile", "offline_access"}
        assert not (reserved & set(microsoft.SCOPES))


class TestTimes:
    def test_bare_date_becomes_local_midnight(self):
        assert due_body("2026-09-25") == {"dateTime": "2026-09-25T00:00:00", "timeZone": "UTC"}

    def test_datetime_is_kept_as_is(self):
        assert due_body("2026-09-25T10:00:00") == {"dateTime": "2026-09-25T10:00:00", "timeZone": "UTC"}


class TestFormatting:
    def test_open_task(self):
        line = format_task({"id": "t1", "title": "Buy milk", "status": "notStarted"})
        assert line.startswith("[ ]") and "Buy milk" in line and "id t1" in line

    def test_completed_task_is_flagged(self):
        line = format_task({"id": "t2", "title": "Done thing", "status": "completed"})
        assert line.startswith("[x]")

    def test_due_date_is_shown(self):
        line = format_task({
            "id": "t3", "title": "Renew passport", "status": "notStarted",
            "dueDateTime": {"dateTime": "2026-10-01T00:00:00.0000000", "timeZone": "UTC"},
        })
        assert "due 2026-10-01" in line


class TestAccounts:
    def test_label_cannot_escape_the_token_directory(self, settings):
        with pytest.raises(MicrosoftError):
            microsoft.token_path(settings, "../../evil")

    def test_no_accounts_is_a_clear_error(self, settings):
        with pytest.raises(MicrosoftError, match="qm auth microsoft"):
            microsoft.resolve_account(settings, None)

    @staticmethod
    def _save(settings, label: str) -> None:
        settings.tokens_dir.mkdir(parents=True, exist_ok=True)
        # MSAL owns this file's real shape; the code under test never parses
        # it directly, only checks whether it exists and lists its stem.
        (settings.tokens_dir / f"microsoft-{label}.json").write_text("{}")

    def test_unnamed_account_means_the_only_one(self, settings):
        self._save(settings, "personal")
        assert microsoft.resolve_account(settings, None) == "personal"
        assert microsoft.resolve_account(settings, "personal") == "personal"

    def test_ambiguous_when_more_than_one(self, settings):
        self._save(settings, "personal")
        self._save(settings, "school")
        with pytest.raises(MicrosoftError, match="personal, school"):
            microsoft.resolve_account(settings, None)

    def test_naming_an_unknown_account_says_so(self, settings):
        self._save(settings, "personal")
        with pytest.raises(MicrosoftError, match="personal"):
            microsoft.resolve_account(settings, "work")
