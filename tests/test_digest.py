"""The daily digest: taste matching, grouping one show's listings, what comes
back and when, and the rendered message. Ticketmaster, Google and the model
are faked; state.db is real (a temp file)."""

import json
from datetime import date
from pathlib import Path

import pytest

from quartermaster import agent, db, digest
from quartermaster.config import DEFAULTS, Settings, _deep_merge
from quartermaster.digest import calendar_days, classify_events, listings, render
from quartermaster.digest.render import esc

TODAY = date(2026, 10, 3)


def lst(id_, name="Show", acts=("Tool",), day="2026-10-10", venue="The Pageant", **kw) -> dict:
    return {"id": id_, "name": name, "acts": list(acts), "local_date": day, "local_time": "20:00",
            "starts_at": day + "T01:00:00Z", "venue": venue, "city": "St. Louis", "distance_miles": 3.2,
            "url": f"https://tm/{id_}", "onsale_at": "", "presales": [], "band": "local", "bar": "low", **kw}


# --- Taste ------------------------------------------------------------------------


def test_matches_any_billed_act_and_whole_words_only():
    assert digest.matches_taste(lst("1", acts=("Opener", "aespa")), {"aespa"}, "")
    interests = "- k-pop (aespa, le sserafim)\n- i like my toolbox"
    assert digest.matches_taste(lst("2", acts=("LE SSERAFIM",)), set(), interests)
    assert not digest.matches_taste(lst("3", acts=("Tool",)), set(), interests)
    assert not digest.matches_taste(lst("4", acts=()), {"aespa"}, "k-pop")


def test_not_interested_section_never_counts_as_a_match():
    text = "## Confirmed\n- aespa\n\n## Not interested\n- St. Louis Blues games\n\n## Later\n- Tool\n"
    kept = digest.positive_interests(text).lower()
    assert "blues" not in kept and "aespa" in kept and "tool" in kept
    assert not digest.matches_taste(lst("1", acts=("St. Louis Blues",)), set(), kept)


# --- One show, several listings ------------------------------------------------------


LCS = [
    lst("d1", "LCS SUMMER FINALS", ("League of Legends",), "2026-10-03", "Gas South Arena"),
    lst("bundle", "LCS SUMMER FINALS - TWO-DAY BUNDLE", ("League of Legends",), "2026-10-03", "Gas South Arena"),
    lst("d2", "LCS SUMMER FINALS", ("League of Legends",), "2026-10-04", "Gas South Arena"),
]


def test_day_passes_and_a_bundle_are_one_item_with_every_link():
    groups = listings.group(LCS + [lst("other", acts=("Tool",))])
    assert sorted(len(g) for g in groups) == [1, 3]
    it = listings.item(next(g for g in groups if len(g) == 3))
    assert it["acts"] == ["League of Legends"] and it["title"] == "LCS Summer Finals"
    assert (it["first_date"], it["last_date"]) == ("2026-10-03", "2026-10-04")
    assert [l["label"] for l in it["listings"]] == ["Sat Oct 3", "Two-day Bundle", "Sun Oct 4"]
    assert [l["url"] for l in it["listings"]] == ["https://tm/d1", "https://tm/bundle", "https://tm/d2"]


def test_the_same_act_weeks_apart_or_at_another_venue_stays_separate():
    groups = listings.group([lst("a", day="2026-10-10"), lst("b", day="2026-10-30"),
                             lst("c", day="2026-10-10", venue="Enterprise Center")])
    assert len(groups) == 3


def test_a_shouted_title_is_tidied_and_the_act_not_repeated():
    it = listings.item([lst("x", "JOHN SUMMIT - CTRL ESCAPE ARENA TOUR", ("John Summit",))])
    assert it["title"] == "Ctrl Escape Arena Tour" and it["listings"][0]["label"] == "Tickets"
    assert listings.item([lst("y", "Jozzy", ("Jozzy",))])["title"] == ""


# --- What comes back, and when -------------------------------------------------------


def rows(**by_id) -> dict:
    base = {"considered_at": None, "event_shown_at": None}
    return {i: {**base, **r} for i, r in by_id.items()}


def test_new_events_go_to_the_model_once_and_a_shown_one_returns_the_week_of():
    new = [lst("n")]
    passed_over = [lst("p")]
    shown_long_ago = [lst("r", day="2026-10-08")]
    shown_this_week = [lst("w", day="2026-10-08")]
    shown_far_off = [lst("f", day="2026-10-28")]
    known = rows(p={"considered_at": "2026-09-30"},
                 r={"considered_at": "x", "event_shown_at": "2026-09-25T13:00:00+00:00"},
                 w={"considered_at": "x", "event_shown_at": "2026-10-02T13:00:00+00:00"},
                 f={"considered_at": "x", "event_shown_at": "2026-09-25T13:00:00+00:00"})
    fresh, reminders = classify_events([new, passed_over, shown_long_ago, shown_this_week, shown_far_off], known, TODAY)
    assert fresh == [new] and reminders == [shown_long_ago]


def test_calendar_skips_ignored_all_day_entries_but_keeps_real_plans():
    events = [
        {"summary": "Sam's day 🎂", "start": {"date": "2026-10-03"}, "end": {"date": "2026-10-04"}},
        {"summary": "Dinner with Sam", "start": {"dateTime": "2026-10-03T18:30:00-05:00"},
         "end": {"dateTime": "2026-10-03T20:30:00-05:00"}},
        {"summary": "Boo at da Zoo", "start": {"dateTime": "2026-10-08T17:30:00-05:00"},
         "end": {"dateTime": "2026-10-08T21:00:00-05:00"}},
    ]
    days = calendar_days(events, ["SAM"])
    assert days == [
        {"date": "2026-10-03", "events": [{"title": "Dinner with Sam", "all_day": False, "start": "18:30", "end": "20:30"}]},
        {"date": "2026-10-08", "events": [{"title": "Boo at da Zoo", "all_day": False, "start": "17:30", "end": "21:00"}]},
    ]


# --- The message -------------------------------------------------------------------


def test_render_is_skimmable_and_skips_empty_sections():
    item = {**listings.item(LCS), "band": "weekend", "why": "you follow LCS"}
    text = render({
        "date": "2026-10-03",
        "calendar": {"days": [{"date": "2026-10-03", "events": [
            {"title": "Dinner at Kasabi", "all_day": False, "start": "18:30", "end": "20:30"}]}]},
        "onsales": {"items": [], "more": 0},
        "events": {"items": [item]},
        "prices": {"drops": [], "failures": [{"item_name": "Desk", "url": "u"}]},
        "notion": {"stale": []},
    })
    assert text.startswith("## Saturday, October 3\n### 📅 Calendar\n**Today** · Dinner at Kasabi (6:30 pm–8:30 pm)")
    assert "**League of Legends** · LCS Summer Finals — *you follow LCS*" in text
    assert "-# Today – Tomorrow · Gas South Arena, St. Louis · 3 mi · [Sat Oct 3](<https://tm/d1>)" in text
    assert "__Weekend__" in text and "On sale soon" not in text and "Notion" not in text
    assert "-# Couldn't check: Desk" in text


def test_nothing_at_all_renders_nothing():
    assert render({"date": "2026-10-03", "calendar": {"days": []}, "events": {"items": []}}) == ""


def test_names_cant_break_the_formatting():
    assert esc("*NSYNC_") == r"\*NSYNC\_"


# --- A whole run ---------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    vault = tmp_path / "Vault"
    (vault / "System").mkdir(parents=True)
    monkeypatch.setattr(digest.taste, "artist_names", lambda s: {"tool", "league of legends"})
    monkeypatch.setattr(digest.taste, "top_artists_text", lambda s: "1. Tool")
    monkeypatch.setattr(digest.google, "calendar_events", lambda *a, **k: [])
    monkeypatch.setattr(digest.prices, "check_all", lambda s, c: {"drops": [], "failures": []})
    monkeypatch.setattr(digest.stale, "find_stale_pages", lambda s, c: [])
    monkeypatch.setattr("quartermaster.discord_bot.held.quiet", lambda s, now=None: False)  # sent, not held
    return Settings(vault=vault, prefs=_deep_merge(DEFAULTS, {"digest": {"ignore_calendar": ["sam"]}}))


def test_a_run_shows_onsales_once_and_events_the_model_picked(settings, monkeypatch):
    onsale = lst("o1", "TOOL - FEAR INOCULUM", ("Tool",), "2026-12-01", onsale_at="2026-10-10T15:00:00Z")
    nobody = lst("o2", "Somebody", ("Somebody",), onsale_at="2026-10-10T15:00:00Z")
    monkeypatch.setattr(digest.ticketmaster, "onsales_between", lambda s, d, n: [onsale, nobody])
    monkeypatch.setattr(digest.ticketmaster, "events_for_bands",
                        lambda s, n, prefer=None: {"local": [lst("e1"), lst("e2", acts=("Meh",))]})
    asked = []

    async def fake_ask(prompt, profile, cli):
        asked.append(json.loads(prompt.split("Candidates:\n", 1)[1]))
        assert profile.output_schema is digest.PICK_SCHEMA and profile.tools == []
        return agent.Reply(text="", structured={"picks": [{"id": "e1", "why": "your top artist"}, {"id": "zz", "why": "?"}]})

    sent = []
    monkeypatch.setattr(agent, "ask", fake_ask)
    monkeypatch.setattr("quartermaster.discord_bot.send.send_dm", lambda s, text: sent.append(text))

    preview = digest.run_digest(settings, dry_run=True)
    assert "**Tool** · Fear Inoculum" in preview and "Somebody" not in preview
    assert "*your top artist*" in preview and "Meh" not in preview
    assert sent == [] and not settings.digests_dir.exists()

    digest.run_digest(settings)
    assert len(sent) == 1 and sent[0] == preview
    archived = json.loads((settings.digests_dir / f"{date.today().isoformat()}.json").read_text("utf-8"))
    assert archived["onsales"]["items"][0]["listings"][0]["id"] == "o1"
    assert {c["id"] for c in asked[-1]} == {"e1", "e2"}

    # Next morning: the on-sale was shown, both events were considered.
    asked.clear()
    assert digest.run_digest(settings, dry_run=True) == ""
    assert asked == []
    with db.session(settings.db_path) as conn:
        r = db.listings_by_id(conn, ["o1", "e1", "e2"])
        assert r["o1"]["onsale_shown_at"] and r["e1"]["event_times_shown"] == 1 and r["e2"]["event_times_shown"] == 0


def test_a_failed_pick_keeps_the_events_for_tomorrow(settings, monkeypatch):
    monkeypatch.setattr(digest.ticketmaster, "onsales_between", lambda s, d, n: [])
    monkeypatch.setattr(digest.ticketmaster, "events_for_bands", lambda s, n, prefer=None: {"local": [lst("e1")]})

    async def broken(prompt, profile, cli):
        return agent.Reply(text="", error="spend cap")

    monkeypatch.setattr(agent, "ask", broken)
    monkeypatch.setattr(digest.google, "calendar_events", lambda *a, **k: [
        {"summary": "Gym", "start": {"dateTime": "2026-10-03T07:00:00-05:00"}, "end": {"dateTime": "2026-10-03T08:00:00-05:00"}}])
    monkeypatch.setattr("quartermaster.discord_bot.send.send_dm", lambda s, text: None)
    text = digest.run_digest(settings)
    assert "Couldn't check: couldn't pick events (spend cap)" in text and "Gym" in text
    with db.session(settings.db_path) as conn:
        assert db.listings_by_id(conn, ["e1"])["e1"]["considered_at"] is None


def test_a_matinee_and_an_evening_show_are_labelled_by_time():
    it = listings.item([lst("m", local_time="15:00"), {**lst("e"), "local_time": "19:30"}])
    assert [l["label"] for l in it["listings"]] == ["Sat Oct 10, 3 pm", "Sat Oct 10, 7:30 pm"]


def test_each_band_offers_its_favourites_first_then_the_soonest_and_the_rest_wait():
    soon = [[lst(f"s{i}", acts=("Somebody",), day="2026-10-04")] for i in range(100)]
    fav = [lst("fav", acts=("Tool",), day="2026-10-30")]
    far = [[{**lst("w1", acts=("Nobody",)), "band": "weekend"}]]
    offered = digest.offer([*soon, fav, *far], lambda e: "Tool" in e["acts"])
    ids = [g[0]["id"] for g in offered]
    assert len(ids) == digest.MAX_OFFERED_PER_BAND + 1
    assert ids[0] == "fav" and "w1" in ids


def test_without_favourites_a_band_is_offered_across_the_month_not_just_tonight():
    tonight = [[lst(f"t{i}", acts=("X",), day="2026-10-03")] for i in range(200)]
    later = [[lst(f"l{d}", acts=("Y",), day=f"2026-10-{d:02d}")] for d in range(10, 31)]
    days = {g[0]["local_date"] for g in digest.offer([*tonight, *later], lambda e: False)}
    assert "2026-10-30" in days and len(days) == 22


def test_passes_named_up_front_are_labelled_by_the_part_that_differs():
    it = listings.item([
        lst("2d", "2 Day Pass: Ganja White Night - Wobbleween", ("Ganja White Night",), "2026-10-30"),
        lst("fri", "Friday Pass: Ganja White Night - Wobbleween", ("Ganja White Night",), "2026-10-30"),
        lst("sat", "Saturday Pass: Ganja White Night - Wobbleween", ("Ganja White Night",), "2026-10-31"),
    ])
    assert it["title"] == "Wobbleween"
    assert [l["label"] for l in it["listings"]] == ["2 Day Pass", "Friday Pass", "Saturday Pass"]


def test_a_test_run_dms_with_a_note_and_marks_nothing_then_reset_forgets_a_real_run(settings, monkeypatch):
    monkeypatch.setattr(digest.ticketmaster, "onsales_between", lambda s, d, n: [
        lst("o1", "TOOL - FEAR INOCULUM", ("Tool",), "2026-12-01", onsale_at="2026-10-10T15:00:00Z")])
    monkeypatch.setattr(digest.ticketmaster, "events_for_bands", lambda s, n, prefer=None: {"local": [lst("e1")]})

    async def fake_ask(prompt, profile, cli):
        return agent.Reply(text="", structured={"picks": [{"id": "e1", "why": "your top artist"}]})

    sent = []
    monkeypatch.setattr(agent, "ask", fake_ask)
    monkeypatch.setattr("quartermaster.discord_bot.send.send_dm", lambda s, text: sent.append(text))

    text = digest.run_digest(settings, test=True)
    assert sent == [f"{digest.TEST_NOTE}\n{text}"] and "Fear Inoculum" in text
    assert not settings.digests_dir.exists()
    assert digest.run_digest(settings, dry_run=True) == text  # nothing was marked

    digest.run_digest(settings)
    assert digest.run_digest(settings, dry_run=True) == ""  # a real run marks
    with db.session(settings.db_path) as conn:
        assert db.reset_digest_marks(conn) == {"listings": 2, "surfaced": 0}
        assert db.listings_by_id(conn, ["e1"])["e1"]["first_seen"]  # observations stay
    assert digest.run_digest(settings, dry_run=True) == text
