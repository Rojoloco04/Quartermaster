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

    def test_every_job_runs_windowless(self, settings):
        # qm.exe is a console program: registered as it, each job opened a
        # Windows Terminal at its hour (the 18:00 digest, 2026-09-22).
        for task in build_tasks(settings, "daily"):
            assert task.command[0].endswith("pythonw.exe") and task.command[1:3] == ["-m", "quartermaster.cli"]

    def test_notion_sync_runs_daily_before_the_other_jobs(self, settings):
        # The stale-page scan reads the mirror; it must not be days old.
        sync = next(t for t in build_tasks(settings, "weekly") if t.name == SYNC_TASK)
        assert sync.command[-1] == "sync"
        assert sync.schedule_args == ["/sc", "daily", "/st", f"{SYNC_HOUR:02d}:00"]
        assert SYNC_HOUR < PRESALE_HOUR


def test_service_task_runs_windowless_forever_and_once():
    import xml.etree.ElementTree as ET

    from quartermaster.schedule import service_xml

    xml = service_xml(r"DESK&TOP\you", r"C:\q\.venv\Scripts\pythonw.exe", r"C:\q")
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    root = ET.fromstring(xml.split("?>", 1)[1])  # parses, with the user name escaped
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", ns).text == r"DESK&TOP\you"
    settings = root.find("t:Settings", ns)
    assert settings.find("t:ExecutionTimeLimit", ns).text == "PT0S"  # the default kills it after 72h
    assert settings.find("t:MultipleInstancesPolicy", ns).text == "IgnoreNew"
    assert settings.find("t:StopIfGoingOnBatteries", ns).text == "false"
    exec_ = root.find("t:Actions/t:Exec", ns)
    assert exec_.find("t:Command", ns).text.endswith("pythonw.exe")
    assert exec_.find("t:Arguments", ns).text == "-m quartermaster.cli serve"
