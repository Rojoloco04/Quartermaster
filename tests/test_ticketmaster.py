"""Ticketmaster integration: distance math, band bucketing, and the mute-id
scheme - all pure, none of it needs a live network call to verify."""

from pathlib import Path

import pytest

from quartermaster import mutes
from quartermaster.config import Settings
from quartermaster.integrations import ticketmaster
from quartermaster.integrations.ticketmaster import _haversine_miles, item_id


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        vault=tmp_path / "Vault",
        claude_cli=None,
        notion_token=None,
        discord_bot_token=None,
        discord_owner_id=None,
        ticketmaster_api_key="x",
        prefs={
            "home": {"geopoint": "9yzg", "latitude": 38.6270, "longitude": -90.1994},
            "events": {
                "bands": [
                    {"name": "local", "min_miles": 0, "max_miles": 60, "bar": "low"},
                    {"name": "day_trip", "min_miles": 60, "max_miles": 250, "bar": "medium"},
                    {"name": "weekend", "min_miles": 250, "max_miles": 500, "bar": "high"},
                ],
                "window_days": 60,
            },
        },
    )


def event(id_: str, lat: float, lon: float, attraction: str | None = None) -> dict:
    return {
        "id": id_,
        "name": f"Show {id_}",
        "url": f"https://example.com/{id_}",
        "starts_at": "2026-10-01T20:00:00Z",
        "onsale_start": "2026-09-01T10:00:00Z",
        "venue_name": "Some Venue",
        "city": "Somewhere",
        "lat": lat,
        "lon": lon,
        "attraction": attraction,
    }


class TestHaversine:
    def test_same_point_is_zero(self):
        assert _haversine_miles(38.6270, -90.1994, 38.6270, -90.1994) == pytest.approx(0.0, abs=0.01)

    def test_stl_to_chicago(self):
        # St. Louis to Chicago is commonly cited as ~260 road miles / ~257 great-circle miles.
        distance = _haversine_miles(38.6270, -90.1994, 41.8781, -87.6298)
        assert distance == pytest.approx(257, abs=10)


class TestBandBucketing:
    def test_events_land_in_the_band_their_true_distance_matches(self, settings, monkeypatch):
        # STL home point. Chicago (~257mi) should land in "weekend" (250-500),
        # not "day_trip", even though a day_trip-radius query (radius=250)
        # would never see it and a weekend-radius query (radius=500) would.
        local_event = event("local1", 38.63, -90.20)  # basically at home
        chicago_event = event("chi1", 41.8781, -87.6298)

        def fake_search(settings, *, radius_miles, start=None, end=None, onsale_start=None, onsale_end=None):
            if radius_miles == 60:
                return [local_event]
            if radius_miles == 250:
                return [local_event]  # still within a 250mi radius query
            return [local_event, chicago_event]  # radius 500

        monkeypatch.setattr(ticketmaster, "search_events", fake_search)

        banded = ticketmaster.events_for_bands(settings, window_days=60)

        assert [e["id"] for e in banded["local"]] == ["local1"]
        assert banded["day_trip"] == []
        assert [e["id"] for e in banded["weekend"]] == ["chi1"]

    def test_an_event_never_appears_in_two_bands(self, settings, monkeypatch):
        # A boundary-adjacent event returned by more than one radius query
        # must still end up in exactly one band's list.
        borderline = event("b1", 41.8781, -87.6298)

        def fake_search(settings, *, radius_miles, start=None, end=None, onsale_start=None, onsale_end=None):
            return [borderline] if radius_miles >= 250 else []

        monkeypatch.setattr(ticketmaster, "search_events", fake_search)

        banded = ticketmaster.events_for_bands(settings, window_days=60)
        total = sum(len(v) for v in banded.values())
        assert total == 1

    def test_events_with_no_venue_coordinates_are_dropped_not_crashed(self, settings, monkeypatch):
        no_coords = {**event("nc1", 0, 0), "lat": None, "lon": None}

        monkeypatch.setattr(ticketmaster, "search_events", lambda *a, **k: [no_coords])

        banded = ticketmaster.events_for_bands(settings, window_days=60)
        assert sum(len(v) for v in banded.values()) == 0

    def test_a_band_is_capped_rather_than_dumping_everything_on_the_model(self, settings, monkeypatch):
        # A farther band can legitimately return close to Ticketmaster's
        # 1000-result deep-paging cap - all of it must not land in the prompt.
        many = [event(f"e{i}", 38.63, -90.20) for i in range(200)]

        monkeypatch.setattr(ticketmaster, "search_events", lambda *a, **k: many)

        banded = ticketmaster.events_for_bands(settings, window_days=60)
        assert len(banded["local"]) == ticketmaster.MAX_EVENTS_PER_BAND


class TestMuteScheme:
    def test_event_with_artist_uses_the_documented_scheme(self):
        # mutes.py's own docstring gives `event:artist/Tool` as the example
        # of a mute that silences every show by that artist.
        assert item_id(event("e1", 0, 0, attraction="Tool")) == "event:artist/Tool/e1"

    def test_event_without_artist_falls_back_to_id(self):
        assert item_id(event("e2", 0, 0, attraction=None)) == "event:id/e2"

    def test_muting_the_artist_scope_silences_every_show(self):
        mute_list = [mutes.Mute(item_id="event:artist/Tool", note="")]
        assert mutes.is_muted(item_id(event("e1", 0, 0, "Tool")), mute_list)
        assert mutes.is_muted(item_id(event("e2", 0, 0, "Tool")), mute_list)
        assert not mutes.is_muted(item_id(event("e3", 0, 0, "Other Band")), mute_list)

    def test_presale_and_event_mutes_are_independent_namespaces(self):
        # Muting the weekly-digest listing for an artist must not also
        # silence their same-day presale ping, or vice versa - they're
        # different asks (see ticketmaster.item_id).
        e = event("e1", 0, 0, attraction="Tool")
        mute_list = [mutes.Mute(item_id="event:artist/Tool", note="")]
        assert mutes.is_muted(item_id(e), mute_list)
        assert not mutes.is_muted(item_id(e, "presale"), mute_list)
