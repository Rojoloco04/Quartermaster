"""state.db: price history and the events-seen ledger.

Nothing here is the only copy of anything - db.py's own rule - so these tests
care about the two properties that rule depends on: a failed price check must
never look like "no change", and first_seen must never move once set.
"""

from pathlib import Path

from quartermaster import db


def test_latest_price_ignores_failed_checks(tmp_path: Path):
    with db.session(tmp_path / "state.db") as conn:
        db.record_price_check(conn, "https://x/1", "Widget", 1000, "USD", ok=True)
        db.record_price_check(conn, "https://x/1", "Widget", None, "USD", ok=False, note="http 503")

        latest = db.latest_price(conn, "https://x/1")
        assert latest["price_cents"] == 1000, "a failed check must not be mistaken for a price"


def test_latest_price_is_the_most_recent_successful_check(tmp_path: Path):
    with db.session(tmp_path / "state.db") as conn:
        db.record_price_check(conn, "https://x/1", "Widget", 2000, "USD", ok=True)
        db.record_price_check(conn, "https://x/1", "Widget", 1500, "USD", ok=True)

        assert db.latest_price(conn, "https://x/1")["price_cents"] == 1500


def test_no_successful_check_means_no_latest_price(tmp_path: Path):
    with db.session(tmp_path / "state.db") as conn:
        db.record_price_check(conn, "https://x/1", "Widget", None, "USD", ok=False, note="couldn't check")
        assert db.latest_price(conn, "https://x/1") is None


def test_shown_count_starts_at_zero_and_does_not_mutate(tmp_path: Path):
    with db.session(tmp_path / "state.db") as conn:
        assert db.shown_count(conn, "event:id/e1") == 0
        assert db.shown_count(conn, "event:id/e1") == 0, "reading shown_count must never itself count as a showing"


def test_record_surfaced_increments_and_shown_count_agrees(tmp_path: Path):
    with db.session(tmp_path / "state.db") as conn:
        assert db.record_surfaced(conn, "event:id/e1", "event", "Show") == 1
        assert db.record_surfaced(conn, "event:id/e1", "event", "Show") == 2
        assert db.shown_count(conn, "event:id/e1") == 2


def test_record_event_seen_keeps_the_original_first_seen(tmp_path: Path):
    with db.session(tmp_path / "state.db") as conn:
        db.record_event_seen(conn, "e1", "Show", "2026-10-01T20:00:00Z", "local", "Some Venue")
        first = conn.execute("SELECT first_seen FROM events_seen WHERE event_id = ?", ("e1",)).fetchone()

        db.record_event_seen(conn, "e1", "Show", "2026-10-01T20:00:00Z", "local", "Some Venue")
        second = conn.execute("SELECT first_seen FROM events_seen WHERE event_id = ?", ("e1",)).fetchone()

        assert first["first_seen"] == second["first_seen"]
