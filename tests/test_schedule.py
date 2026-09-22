"""Task Scheduler wiring: only the pure command-building is tested here -
`install`/`remove`/`status` shell out to schtasks.exe and aren't worth faking."""

from pathlib import Path

import pytest

from quartermaster.config import Settings
from quartermaster.schedule import DIGEST_TASK, PRESALE_HOUR, PRESALE_TASK, SYNC_HOUR, SYNC_TASK, build_tasks


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        vault=tmp_path / "Vault",
        claude_cli=None,
        notion_token=None,
        discord_bot_token=None,
        discord_owner_id=None,
        prefs={"digest": {"weekday": "sunday", "hour": 18, "calendar_days_ahead": 7}},
    )


class TestBuildTasks:
    def test_rejects_an_unknown_cadence(self, settings):
        with pytest.raises(ValueError, match="daily.*weekly"):
            build_tasks(settings, "monthly")

    def test_weekly_cadence_fires_on_the_configured_weekday_and_hour(self, settings):
        tasks = build_tasks(settings, "weekly")
        digest = next(t for t in tasks if t.name == DIGEST_TASK)
        assert digest.schedule_args == ["/sc", "weekly", "/d", "SUN", "/st", "18:00"]

    def test_daily_cadence_drops_the_weekday(self, settings):
        tasks = build_tasks(settings, "daily")
        digest = next(t for t in tasks if t.name == DIGEST_TASK)
        assert digest.schedule_args == ["/sc", "daily", "/st", "18:00"]
        assert "/d" not in digest.schedule_args

    def test_presale_task_is_always_daily_regardless_of_digest_cadence(self, settings):
        for cadence in ("daily", "weekly"):
            tasks = build_tasks(settings, cadence)
            presale = next(t for t in tasks if t.name == PRESALE_TASK)
            assert presale.schedule_args == ["/sc", "daily", "/st", f"{PRESALE_HOUR:02d}:00"]

    def test_commands_invoke_qm_digest_and_qm_presale_check(self, settings):
        tasks = build_tasks(settings, "weekly")
        digest = next(t for t in tasks if t.name == DIGEST_TASK)
        presale = next(t for t in tasks if t.name == PRESALE_TASK)
        assert digest.command[-1] == "digest"
        assert presale.command[-1] == "presale-check"
        assert digest.command[0].endswith("qm.exe")

    def test_notion_sync_runs_daily_before_the_other_jobs(self, settings):
        # The stale-page scan reads the mirror; it must not be days old.
        sync = next(t for t in build_tasks(settings, "weekly") if t.name == SYNC_TASK)
        assert sync.command[-1] == "sync"
        assert sync.schedule_args == ["/sc", "daily", "/st", f"{SYNC_HOUR:02d}:00"]
        assert SYNC_HOUR < PRESALE_HOUR
