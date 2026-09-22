"""The presale ping: taste-filtered and capped.

Regression: with neither, one morning's check DM'd ~1000 events - every public
on-sale within 500 miles.
"""

from pathlib import Path

from quartermaster import digest
from quartermaster.config import DEFAULTS, Settings


def ev(i: int, *acts: str) -> dict:
    return {"id": f"e{i}", "name": f"Show {i}", "attraction": acts[0] if acts else None,
            "attractions": list(acts), "starts_at": "2026-10-01", "url": ""}


def test_matches_spotify_artist_on_any_billed_act():
    assert digest.matches_taste(ev(1, "Opener", "aespa"), {"aespa"}, "")


def test_matches_whole_words_in_interests_only():
    interests = "- k-pop (aespa, le sserafim)\n- i like my toolbox"
    assert digest.matches_taste(ev(1, "LE SSERAFIM"), set(), interests)
    assert not digest.matches_taste(ev(2, "Tool"), set(), interests)


def test_unmatched_and_actless_events_are_dropped():
    assert not digest.matches_taste(ev(1, "Somebody Else"), {"aespa"}, "k-pop")
    assert not digest.matches_taste(ev(2), {"aespa"}, "k-pop")


def test_presale_ping_is_filtered_and_capped(tmp_path: Path, monkeypatch):
    settings = Settings(vault=tmp_path, prefs=DEFAULTS)
    events = [ev(i, "aespa") for i in range(50)] + [ev(100 + i, "Nobody") for i in range(900)]
    monkeypatch.setattr(digest.ticketmaster, "presales_starting", lambda s, d: events)
    monkeypatch.setattr(digest, "_taste", lambda s: ({"aespa"}, ""))
    text = digest.run_presale_check(settings, dry_run=True)
    lines = text.splitlines()[1:]
    assert len(lines) == digest.MAX_PRESALE_LINES + 1
    assert lines[-1] == "...and 40 more."
    assert "Nobody" not in text
