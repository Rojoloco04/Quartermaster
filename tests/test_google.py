"""Google integration: the parts that don't need a network or a token."""

import base64
import json
from pathlib import Path

import pytest

from quartermaster.config import Settings
from quartermaster.integrations import google
from quartermaster.integrations.google import (
    GoogleError,
    event_time_body,
    extract_body,
    format_event,
    parse_when,
)


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    s = Settings(
        vault=tmp_path / "Vault", claude_cli=None, notion_token=None,
        discord_bot_token=None, discord_owner_id=None,
    )
    monkeypatch.setattr(Settings, "tokens_dir", property(lambda self: tmp_path / "tokens"))
    return s


class TestTimes:
    def test_bare_date_is_local_midnight(self):
        moment = parse_when("2026-09-25")
        assert (moment.hour, moment.minute) == (0, 0)
        assert moment.tzinfo is not None

    def test_bare_end_date_includes_the_whole_day(self):
        assert parse_when("2026-09-25", end_of_day=True).day == 26

    def test_offset_is_kept(self):
        assert parse_when("2026-09-25T10:00:00Z").utcoffset().total_seconds() == 0

    def test_garbage_is_a_clear_error(self):
        with pytest.raises(GoogleError, match="YYYY-MM-DD"):
            parse_when("next tuesday")

    def test_bare_date_makes_an_all_day_event(self):
        assert event_time_body("2026-09-25") == {"date": "2026-09-25"}

    def test_datetime_makes_a_timed_event(self):
        assert "dateTime" in event_time_body("2026-09-25T10:00")


class TestFormatting:
    def test_timed_event(self):
        line = format_event({
            "id": "e1", "summary": "Dentist", "location": "Main St",
            "start": {"dateTime": "2026-09-25T10:00:00-05:00"},
            "end": {"dateTime": "2026-09-25T11:00:00-05:00"},
        }, "personal/Home")
        assert "10:00-11:00" in line and "Dentist" in line and "Main St" in line
        assert "id e1" in line

    def test_all_day_event(self):
        line = format_event({"id": "e2", "start": {"date": "2026-09-25"}, "end": {"date": "2026-09-26"}})
        assert "all day" in line and "(no title)" in line


class TestBodies:
    def test_prefers_plain_text(self):
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("plain wins")}},
            {"mimeType": "text/html", "body": {"data": b64("<p>html loses</p>")}},
        ]}
        assert extract_body(payload) == "plain wins"

    def test_falls_back_to_stripped_html(self):
        payload = {"mimeType": "text/html", "body": {"data": b64(
            "<html><style>p{}</style><p>Hello&amp;bye</p><script>x()</script></html>"
        )}}
        assert extract_body(payload) == "Hello&bye"

    def test_nested_parts_are_found(self):
        payload = {"mimeType": "multipart/mixed", "parts": [{"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("deep")}},
        ]}]}
        assert extract_body(payload) == "deep"


class TestAccounts:
    def test_label_cannot_escape_the_token_directory(self, settings):
        with pytest.raises(GoogleError):
            google.token_path(settings, "../../evil")

    def test_no_accounts_is_a_clear_error(self, settings):
        with pytest.raises(GoogleError, match="qm auth google"):
            google.resolve_accounts(settings, None, "gmail")

    @staticmethod
    def _save(settings, label: str, *services: str) -> None:
        settings.tokens_dir.mkdir(parents=True, exist_ok=True)
        scopes = [google.SERVICE_SCOPES[s] for s in services]
        (settings.tokens_dir / f"google-{label}.json").write_text(json.dumps({"scopes": scopes}))

    def test_unnamed_account_means_all_with_that_service(self, settings):
        self._save(settings, "personal", "calendar", "gmail")
        self._save(settings, "school", "gmail")
        assert google.resolve_accounts(settings, None, "gmail") == ["personal", "school"]
        # A Gmail-only account is skipped for calendar, not reported as an error.
        assert google.resolve_accounts(settings, None, "calendar") == ["personal"]
        assert google.resolve_accounts(settings, "school", "gmail") == ["school"]
        with pytest.raises(GoogleError, match="personal, school"):
            google.resolve_accounts(settings, "work", "gmail")

    def test_naming_an_account_for_a_service_it_lacks_says_so(self, settings):
        self._save(settings, "school", "gmail")
        with pytest.raises(GoogleError, match="wasn't granted calendar"):
            google.resolve_accounts(settings, "school", "calendar")

    def test_granted_scopes_accepts_list_and_string(self):
        gmail = google.SERVICE_SCOPES["gmail"]
        # oauthlib hands back a list; the raw token response is a string.
        assert google.granted_scopes({"scope": [gmail, "openid"]}) == [gmail]
        assert google.granted_scopes({"scope": f"openid {gmail}"}) == [gmail]
        assert google.granted_scopes({}) == []

    def test_account_services_reads_the_granted_scopes(self, settings):
        self._save(settings, "school", "gmail")
        assert google.account_services(settings, "school") == ["gmail"]
        assert google.account_services(settings, "missing") == []
