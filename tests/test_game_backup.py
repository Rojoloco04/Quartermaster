"""The nightly game-server backup, against temp folders standing in for the
server dirs and the backup drive. No real server runs."""

import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from quartermaster import game_backup
from quartermaster.config import Settings
from quartermaster.game_backup import backup_game, files_of, last_backup, run_backup
from quartermaster.integrations import minecraft, satisfactory
from quartermaster.schedule import BACKUP_TASK, build_tasks


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(vault=tmp_path / "Vault", prefs={
        "minecraft": {"dir": str(tmp_path / "mc")}, "satisfactory": {"dir": str(tmp_path / "sf")},
        "backups": {"dir": str(tmp_path / "F" / "Backups"), "keep": 3},
        "digest": {"weekday": "sunday", "hour": 18},
    })


def fake_game(src: Path):
    """A game whose whole state is one folder."""
    return SimpleNamespace(backup_sources=lambda s: [(src.parent, src.name)] if src.exists() else [])


def at(minute: int) -> datetime:
    return datetime(2026, 10, 4, 5, minute)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_zip_holds_the_sources_and_the_source_is_untouched(settings, tmp_path):
    src = tmp_path / "game" / "world"
    a = write(src / "level.dat", "L")
    write(src / "region" / "r.0.0.mca", "R")
    before = a.stat().st_mtime_ns

    line = backup_game(settings, "g", fake_game(src), now=at(0))

    assert "backed up 2 files" in line and "2026-10-04_050000.zip" in line
    with zipfile.ZipFile(tmp_path / "F" / "Backups" / "g" / "2026-10-04_050000.zip") as z:
        assert sorted(z.namelist()) == ["world/level.dat", "world/region/r.0.0.mca"]
        assert z.read("world/region/r.0.0.mca") == b"R"
    assert a.read_text() == "L" and a.stat().st_mtime_ns == before


def test_unchanged_writes_nothing_and_a_change_writes_a_new_zip(settings, tmp_path):
    src = tmp_path / "game" / "world"
    write(src / "level.dat", "L")
    backup_game(settings, "g", fake_game(src), now=at(0))

    assert "unchanged since 2026-10-04 05:00" in backup_game(settings, "g", fake_game(src), now=at(1))
    write(src / "level.dat", "L2")
    assert "backed up" in backup_game(settings, "g", fake_game(src), now=at(2))
    assert [p.name for p in game_backup.zips(tmp_path / "F" / "Backups" / "g")] == [
        "2026-10-04_050000.zip", "2026-10-04_050200.zip"]


def test_only_the_newest_keep_zips_stay(settings, tmp_path):
    src = tmp_path / "game" / "world"
    for minute in range(5):
        write(src / "level.dat", str(minute))
        backup_game(settings, "g", fake_game(src), now=at(minute))
    names = [p.name for p in game_backup.zips(tmp_path / "F" / "Backups" / "g")]
    assert names == ["2026-10-04_050200.zip", "2026-10-04_050300.zip", "2026-10-04_050400.zip"]


def test_a_failed_copy_leaves_no_zip_and_no_partial(settings, tmp_path, monkeypatch):
    src = tmp_path / "game" / "world"
    write(src / "level.dat", "L")

    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(zipfile.ZipFile, "write", boom)
    with pytest.raises(OSError):
        backup_game(settings, "g", fake_game(src), now=at(0))
    assert list((tmp_path / "F" / "Backups" / "g").iterdir()) == []


def test_the_hold_wraps_the_copy(settings, tmp_path):
    src = tmp_path / "game" / "world"
    write(src / "level.dat", "L")
    events = []

    @contextmanager
    def hold(s):
        events.append("off")
        yield
        events.append("on")

    game = fake_game(src)
    game.backup_hold = hold
    backup_game(settings, "g", game, now=at(0))
    assert events == ["off", "on"]


def test_one_game_failing_doesnt_stop_the_others(settings, tmp_path):
    src = tmp_path / "game" / "world"
    write(src / "level.dat", "L")

    def broken(s):
        raise RuntimeError("no RCON")

    games = {"bad": SimpleNamespace(backup_sources=broken), "good": fake_game(src),
             "none": SimpleNamespace(), "empty": fake_game(tmp_path / "missing")}
    ok, summary = run_backup(settings, games)
    assert not ok
    assert "bad: FAILED: no RCON" in summary and "good: backed up 1 files" in summary
    assert "empty: nothing to back up yet" in summary and "none" not in summary


def test_a_missing_backup_drive_is_a_failure(settings, monkeypatch):
    monkeypatch.setitem(settings.prefs, "backups", {"dir": "Q:\\nowhere", "keep": 3})
    ok, summary = run_backup(settings, {})
    assert not ok and "Q:\\" in summary


def test_last_backup_for_the_servers_page(settings, tmp_path):
    assert last_backup(settings, "g").startswith("No backup yet")
    src = tmp_path / "game" / "world"
    write(src / "level.dat", "L")
    backup_game(settings, "g", fake_game(src), now=at(0))
    assert last_backup(settings, "g").startswith("Last backup 2026-10-04 05:00, 1 kept")


def test_minecraft_sources_skip_the_session_lock_and_include_who_may_join(settings, tmp_path):
    assert minecraft.backup_sources(settings) == []  # not set up
    mc = tmp_path / "mc"
    write(mc / "qm-server.json", "{}")
    write(mc / "server.properties", "level-name=realm\n")
    write(mc / "realm" / "level.dat", "L")
    write(mc / "realm" / "session.lock", "locked")
    write(mc / "realm_nether" / "level.dat", "N")
    write(mc / "ops.json", "[]")
    write(mc / "paper.jar", "big")
    names = [name for _, name in files_of(minecraft.backup_sources(settings))]
    assert names == ["ops.json", "qm-server.json", "realm/level.dat", "realm_nether/level.dat", "server.properties"]


def test_minecraft_hold_flushes_and_pauses_autosave_only_while_running(settings, monkeypatch):
    sent = []
    monkeypatch.setattr(minecraft, "rcon", lambda s, cmd, timeout=5: sent.append(cmd) or "")
    monkeypatch.setattr(minecraft, "is_running", lambda s: False)
    with minecraft.backup_hold(settings):
        pass
    assert sent == []

    monkeypatch.setattr(minecraft, "is_running", lambda s: True)
    with pytest.raises(ValueError):
        with minecraft.backup_hold(settings):
            raise ValueError("copy failed")
    assert sent == ["save-off", "save-all flush", "save-on"]


def test_satisfactory_sources_are_the_servers_not_the_owners_client_saves(settings, tmp_path, monkeypatch):
    root = tmp_path / "SaveGames"
    monkeypatch.setattr(satisfactory, "save_root", lambda: root)
    assert satisfactory.backup_sources(settings) == []
    write(root / "server" / "Factory.sav", "S")
    write(root / "blueprints" / "Factory" / "belt.sbp", "B")
    write(root / "ServerSettings.7777.sav", "claim")
    write(root / "76561198000000000" / "client.sav", "mine")
    write(tmp_path / "sf" / "qm-server.json", "{}")
    names = [name for _, name in files_of(satisfactory.backup_sources(settings))]
    assert names == ["ServerSettings.7777.sav", "blueprints/Factory/belt.sbp", "qm-server.json", "server/Factory.sav"]


def test_scheduled_daily_early_morning(settings):
    task = next(t for t in build_tasks(settings, "daily") if t.name == BACKUP_TASK)
    assert task.command[-1] == "backup" and task.schedule_args == ["/sc", "daily", "/st", "05:00"]
