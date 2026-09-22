"""Last.fm taste signal: parsing, the play-count floor, and keeping the key out of errors."""

from pathlib import Path

import httpx
import pytest

from quartermaster.config import DEFAULTS, Settings
from quartermaster.integrations import lastfm


class Resp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(vault=tmp_path, prefs=DEFAULTS, lastfm_api_key="SECRETKEY123", lastfm_user="someone")


def payload(*rows):
    return {"topartists": {"artist": [{"name": n, "playcount": str(p)} for n, p in rows]}}


def test_artist_names_drops_barely_played_artists(settings, monkeypatch):
    monkeypatch.setattr(lastfm.httpx, "get", lambda *a, **k: Resp(payload(("aespa", 900), ("Once", 2))))
    assert lastfm.artist_names(settings) == {"aespa"}


def test_api_error_is_reported(settings, monkeypatch):
    monkeypatch.setattr(lastfm.httpx, "get", lambda *a, **k: Resp({"error": 10, "message": "Invalid API Key"}))
    with pytest.raises(lastfm.LastfmError, match="Invalid API Key"):
        lastfm.top_artists(settings)


def test_network_errors_never_carry_the_key(settings, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("failed for https://ws.audioscrobbler.com/2.0/?api_key=SECRETKEY123")

    monkeypatch.setattr(lastfm.httpx, "get", boom)
    with pytest.raises(lastfm.LastfmError) as info:
        lastfm.top_artists(settings)
    assert "SECRETKEY123" not in str(info.value) and info.value.__cause__ is None


def test_unconfigured_raises(tmp_path):
    with pytest.raises(RuntimeError, match="lastfm_api_key"):
        lastfm.top_artists(Settings(vault=tmp_path, prefs=DEFAULTS))
