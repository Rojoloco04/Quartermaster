"""The Satisfactory server: claiming on first contact, status wording, the
save-then-shutdown order, and importing an old host's backup. The API is
faked; starting a real server isn't."""

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from quartermaster.config import Settings
from quartermaster.integrations import satisfactory
from quartermaster.integrations.satisfactory import (
    ApiError,
    SatisfactoryError,
    Unreachable,
    describe_state,
    field,
    import_files,
    newest_save,
    save_name,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    folder = tmp_path / "sf"
    (folder / "server" / satisfactory.SERVER_EXE).parent.mkdir(parents=True)
    (folder / "server" / satisfactory.SERVER_EXE).write_bytes(b"")
    (folder / satisfactory.STATE_FILE).write_text(json.dumps({"admin_password": "pw"}))
    return Settings(vault=tmp_path / "Vault", prefs={"satisfactory": {"dir": str(folder), "server_name": "QM"}})


class FakeApi:
    """Answers by function name; records every call."""

    def __init__(self, answers: dict):
        self.answers, self.calls = answers, []

    def __call__(self, function, data=None, token=None, timeout=10):
        self.calls.append((function, data, token))
        answer = self.answers.get(function, {})
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_field_ignores_case():
    assert field({"AuthenticationToken": "t"}, "authenticationToken") == "t"
    assert field({}, "x", "default") == "default"


def test_an_unclaimed_server_is_claimed_with_our_password(settings, monkeypatch):
    fake = FakeApi({"PasswordLogin": ApiError("wrong_password"),
                    "PasswordlessLogin": {"authenticationToken": "initial"},
                    "ClaimServer": {"authenticationToken": "admin"}})
    monkeypatch.setattr(satisfactory, "api", fake)
    assert satisfactory.admin_token(settings) == "admin"
    assert fake.calls[-1] == ("ClaimServer", {"ServerName": "QM", "AdminPassword": "pw"}, "initial")


def test_a_server_claimed_by_someone_else_says_where_the_password_goes(settings, monkeypatch):
    monkeypatch.setattr(satisfactory, "api", FakeApi({
        "PasswordLogin": ApiError("wrong_password"),
        "PasswordlessLogin": ApiError("passwordless_login_not_possible")}))
    with pytest.raises(SatisfactoryError, match="qm-server.json"):
        satisfactory.admin_token(settings)


def test_status_wording(settings, monkeypatch):
    monkeypatch.setattr(satisfactory, "is_running", lambda s: False)
    assert satisfactory.status(settings) == "Stopped."
    monkeypatch.setattr(satisfactory, "is_running", lambda s: True)
    monkeypatch.setattr(satisfactory, "api", FakeApi({"PasswordLogin": Unreachable("refused")}))
    assert satisfactory.status(settings).startswith("Starting up")


def test_status_before_setup(tmp_path):
    s = Settings(vault=tmp_path, prefs={"satisfactory": {"dir": str(tmp_path / "none")}})
    assert satisfactory.status(s) == satisfactory.NOT_SET_UP
    with pytest.raises(SatisfactoryError, match="setup"):
        satisfactory.start(s)


def test_describe_state():
    assert "no save is loaded" in describe_state({"isGameRunning": False})
    line = describe_state({"isGameRunning": True, "activeSessionName": "Base", "numConnectedPlayers": 2,
                           "playerLimit": 4, "techTier": 5, "totalGameDuration": 7300, "averageTickRate": 29.7})
    assert line == "Running Base: 2 of 4 players online; tier 5, 2h played, 30 ticks/s."


def test_save_names_follow_the_games_manual_save_style():
    assert save_name("Base", 0).startswith("Base_") and len(save_name("Base", 0)) == len("Base_ddmmyy-HHMMSS")


def test_stop_saves_before_shutting_down(settings, monkeypatch):
    checks = []
    monkeypatch.setattr(satisfactory, "is_running", lambda s: checks.append(1) or len(checks) == 1)
    fake = FakeApi({"PasswordLogin": {"authenticationToken": "admin"},
                    "QueryServerState": {"serverGameState": {"isGameRunning": True, "activeSessionName": "Base"}}})
    monkeypatch.setattr(satisfactory, "api", fake)
    result = satisfactory.stop(settings)
    assert [c[0] for c in fake.calls] == ["PasswordLogin", "QueryServerState", "SaveGame", "Shutdown"]
    assert fake.calls[2][1]["SaveName"].startswith("Base_") and result.startswith("Stopped; saved as Base_")


def test_import_files_copies_saves_and_blueprints_but_not_the_old_hosts_settings(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "SaveGames"
    (src / "server").mkdir(parents=True)
    (src / "blueprints" / "Base").mkdir(parents=True)
    (src / "ServerSettings.26920.sav").write_bytes(b"old host")
    (src / "server" / "Base_autosave_0.sav").write_bytes(b"new")
    (src / "server" / "Base_autosave_1.sav").write_bytes(b"new")
    (src / "blueprints" / "Base" / "Road.sbp").write_bytes(b"bp")
    (dest / "server").mkdir(parents=True)
    (dest / "server" / "Base_autosave_1.sav").write_bytes(b"mine")  # never overwritten

    saves, skipped = import_files(src, dest)
    assert saves == ["Base_autosave_0"] and skipped == [str(Path("server/Base_autosave_1.sav"))]
    assert (dest / "server" / "Base_autosave_1.sav").read_bytes() == b"mine"
    assert (dest / "blueprints" / "Base" / "Road.sbp").read_bytes() == b"bp"
    assert not list(dest.rglob("ServerSettings*"))


def test_extract_opens_a_tarball_inside_a_zip(tmp_path):
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w:gz") as t:
        info = tarfile.TarInfo("./server/Base_autosave_0.sav")
        info.size = 3
        t.addfile(info, io.BytesIO(b"sav"))
    archive = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("automated_3.tar.gz", tar_bytes.getvalue())
    out = tmp_path / "out"
    out.mkdir()
    satisfactory._extract(archive, out)
    assert (out / "server" / "Base_autosave_0.sav").read_bytes() == b"sav"
    assert not (out / "automated_3.tar.gz").exists()


def test_newest_save_goes_by_the_save_header_date():
    sessions = [{"sessionName": "Base", "saveHeaders": [
        {"saveName": "Base_autosave_0", "saveDateTime": "2026.08.01-14.38.00"},
        {"saveName": "Base_autosave_0_continue", "saveDateTime": "2026.08.27-19.00.00"},
        {"saveName": "Other", "saveDateTime": "2026.09.01-00.00.00"}]}]
    names = {"Base_autosave_0", "Base_autosave_0_continue"}
    assert newest_save(sessions, names) == ("Base", "Base_autosave_0_continue")
    assert newest_save(sessions, {"Missing"}) is None


def test_import_needs_the_server_up(settings, monkeypatch, tmp_path):
    archive = tmp_path / "a.zip"
    archive.write_bytes(b"")
    monkeypatch.setattr(satisfactory, "is_running", lambda s: False)
    with pytest.raises(SatisfactoryError, match="Start the server first"):
        satisfactory.import_save(settings, archive)


def test_import_loads_the_newest_and_makes_its_session_the_autoload(settings, monkeypatch, tmp_path):
    src = tmp_path / "backup"
    (src / "server").mkdir(parents=True)
    (src / "server" / "Base_autosave_0.sav").write_bytes(b"x")
    archive = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(src / "server" / "Base_autosave_0.sav", "server/Base_autosave_0.sav")
    monkeypatch.setattr(satisfactory, "save_root", lambda: tmp_path / "SaveGames")
    monkeypatch.setattr(satisfactory, "is_running", lambda s: True)
    fake = FakeApi({"PasswordLogin": {"authenticationToken": "admin"},
                    "EnumerateSessions": {"sessions": [{"sessionName": "Base", "saveHeaders": [
                        {"saveName": "Base_autosave_0", "saveDateTime": "2026.08.27-19.00.00"}]}]}})
    monkeypatch.setattr(satisfactory, "api", fake)
    satisfactory.import_save(settings, archive)
    assert ("SetAutoLoadSessionName", {"SessionName": "Base"}, "admin") in fake.calls
    assert fake.calls[-1][:2] == ("LoadGame", {"SaveName": "Base_autosave_0", "EnableAdvancedGameSettings": False})
    assert (tmp_path / "SaveGames" / "server" / "Base_autosave_0.sav").exists()


def test_launch_command():
    cmd = satisfactory.launch_command(Path("C:/sf/server") / satisfactory.SERVER_EXE)
    assert cmd.endswith("FactoryServer-Win64-Shipping-Cmd.exe FactoryGame -log -unattended")


def test_startup_chatter_is_hidden_from_the_page():
    from quartermaster.integrations import game_servers

    game = game_servers.GAMES["satisfactory"]
    text = ("[2026.09.22-23.21.16:618][  0]LogStaticMesh: Display: Building static mesh X\n"
            "[2026.09.22-23.21.16:795][  0]LogServer: Display: Server API listening on [::]:7777\n")
    assert game_servers.clean_console(game, text) == text.splitlines(keepends=True)[1]
