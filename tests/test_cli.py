"""Small qm commands: help, and qm web when one is already up."""

from quartermaster import cli
from quartermaster.config import Settings
from quartermaster.ops import procs


def test_help_lists_every_command_once_with_its_options(capsys):
    assert cli.main(["help"]) == 0
    out = capsys.readouterr().out
    assert "digest" in out and "--test" in out and "--reset" in out
    assert "quit, stop" in out and "action: status, setup, start, stop, cmd, op" in out


def test_help_for_one_command_and_an_unknown_one(capsys):
    assert cli.main(["help", "digest"]) == 0
    assert "usage: qm digest" in capsys.readouterr().out
    assert cli.main(["help", "nope"]) == 1


def test_web_already_running_gives_its_link(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Settings, "log_path", property(lambda self: tmp_path / "Logs" / "quartermaster.log"))
    settings = Settings(vault=tmp_path / "Vault")
    settings.log_path.parent.mkdir(parents=True)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "_configure_logging", lambda s: None)
    (settings.log_path.parent / "web.url").write_text("http://127.0.0.1:9000/", encoding="utf-8")
    held = procs.instance_lock(settings.log_path.parent, "web")
    try:
        assert cli.main(["web"]) == 0
    finally:
        held.release()
    assert "already running: http://127.0.0.1:9000/" in capsys.readouterr().out
