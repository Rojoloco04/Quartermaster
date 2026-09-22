"""Spotify integration: the parts that don't need a network or a token."""

from pathlib import Path

import pytest

from quartermaster.config import Settings
from quartermaster.integrations import spotify
from quartermaster.integrations.spotify import SpotifyError


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    s = Settings(
        vault=tmp_path / "Vault", claude_cli=None, notion_token=None,
        discord_bot_token=None, discord_owner_id=None,
    )
    monkeypatch.setattr(Settings, "tokens_dir", property(lambda self: tmp_path / "tokens"))
    return s


class TestScopes:
    def test_scopes_are_read_only(self):
        # This app never manages playlists or controls playback - if a write
        # scope shows up here later, that's a deliberate decision, not a typo.
        for scope in spotify.SCOPES.split():
            assert "read" in scope, f"{scope!r} doesn't look read-only"


class TestAccounts:
    def test_label_cannot_escape_the_token_directory(self, settings):
        with pytest.raises(SpotifyError):
            spotify.token_path(settings, "../../evil")

    def test_no_accounts_is_a_clear_error(self, settings):
        with pytest.raises(SpotifyError, match="qm auth spotify"):
            spotify.resolve_account(settings, None)

    @staticmethod
    def _save(settings, label: str) -> None:
        settings.tokens_dir.mkdir(parents=True, exist_ok=True)
        # spotipy owns this file's real shape; the code under test never
        # parses it directly, only checks whether it exists and lists its stem.
        (settings.tokens_dir / f"spotify-{label}.json").write_text("{}")

    def test_unnamed_account_means_the_only_one(self, settings):
        self._save(settings, "personal")
        assert spotify.resolve_account(settings, None) == "personal"
        assert spotify.resolve_account(settings, "personal") == "personal"

    def test_ambiguous_when_more_than_one(self, settings):
        self._save(settings, "personal")
        self._save(settings, "work")
        with pytest.raises(SpotifyError, match="personal, work"):
            spotify.resolve_account(settings, None)

    def test_naming_an_unknown_account_says_so(self, settings):
        self._save(settings, "personal")
        with pytest.raises(SpotifyError, match="personal"):
            spotify.resolve_account(settings, "work")


class TestTimeRange:
    def test_valid_ranges_pass_through(self):
        for value in ("short_term", "medium_term", "long_term"):
            assert spotify._time_range(value) == value

    def test_invalid_range_is_a_clear_error(self):
        with pytest.raises(SpotifyError, match="short_term"):
            spotify._time_range("last_week")
