"""The Minecraft server: version picking, Java detection, RCON against a fake
server on localhost, and the command allow-list. Starting a real JVM isn't."""

import socket
import struct
import threading
from pathlib import Path

import pytest

from quartermaster.config import Settings
from quartermaster.integrations import minecraft
from quartermaster.integrations.minecraft import (
    MinecraftError,
    default_properties,
    java_major,
    newest_stable,
    read_properties,
    write_properties,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(vault=tmp_path / "Vault", prefs={"minecraft": {"dir": str(tmp_path / "mc"), "memory_gb": 4}})


def test_newest_stable_skips_alpha_builds_and_prereleases():
    versions = {"26.3": ["26.3", "26.3-rc-3"], "26.2": ["26.2"]}
    builds = {"26.3": [{"id": 35, "channel": "ALPHA"}], "26.2": [{"id": 128, "channel": "STABLE"}]}
    version, build = newest_stable(versions, builds.__getitem__)
    assert (version, build["id"]) == ("26.2", 128)


def test_no_stable_build_is_an_error():
    with pytest.raises(MinecraftError, match="no stable"):
        newest_stable({"26.3": ["26.3"]}, lambda v: [{"id": 1, "channel": "ALPHA"}])


@pytest.mark.parametrize("output, major", [
    ('openjdk version "25.0.4" 2026-07-15 LTS', 25),
    ('openjdk version "17.0.18" 2026-01-20', 17),
    ('java version "1.8.0_392"', 8),
    ("nonsense", None),
])
def test_java_major(output, major):
    assert java_major(output) == major


def test_default_properties_whitelist_and_rcon():
    props = default_properties("pw")
    assert props["white-list"] == props["enforce-whitelist"] == props["enable-rcon"] == "true"
    assert props["online-mode"] == "true" and props["rcon.password"] == "pw"


def test_properties_round_trip_ignores_comments(tmp_path):
    path = tmp_path / "server.properties"
    path.write_text("#Minecraft server properties\nmotd=hi=there\nmax-players=20\n")
    props = read_properties(path)
    assert props == {"motd": "hi=there", "max-players": "20"}
    write_properties(path, props)
    assert read_properties(path) == props


def test_setup_needs_the_eula(settings):
    with pytest.raises(MinecraftError, match="EULA"):
        minecraft.setup(settings, accept_eula=False)


class FakeRcon:
    """A one-connection RCON server: checks the password, answers commands."""

    def __init__(self, password: str, replies: dict[str, str]):
        self.password, self.replies, self.received = password, replies, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    @staticmethod
    def _send(conn, request_id, kind, body):
        payload = struct.pack("<ii", request_id, kind) + body.encode() + b"\x00\x00"
        conn.sendall(struct.pack("<i", len(payload)) + payload)

    @staticmethod
    def _recv(conn):
        (length,) = struct.unpack("<i", conn.recv(4))
        data = conn.recv(length)
        request_id, kind = struct.unpack("<ii", data[:8])
        return request_id, kind, data[8:-2].decode()

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            request_id, _, password = self._recv(conn)
            self._send(conn, request_id if password == self.password else -1, 2, "")
            if password != self.password:
                return
            request_id, _, command = self._recv(conn)
            self.received.append(command)
            self._send(conn, request_id, 0, self.replies.get(command, ""))


def configure(settings, port, password="secret"):
    folder = minecraft.server_dir(settings)
    folder.mkdir(parents=True)
    write_properties(folder / "server.properties", {"rcon.port": str(port), "rcon.password": password})


def test_rcon_sends_the_command_and_strips_colour_codes(settings):
    fake = FakeRcon("secret", {"list": "There are §a1§r of a max of 20 players online: Steve"})
    configure(settings, fake.port)
    assert minecraft.rcon(settings, "list") == "There are 1 of a max of 20 players online: Steve"
    assert fake.received == ["list"]


def test_rcon_wrong_password_is_a_clear_error(settings):
    fake = FakeRcon("secret", {})
    configure(settings, fake.port, password="wrong")
    with pytest.raises(MinecraftError, match="refused the password"):
        minecraft.rcon(settings, "list")


def test_rcon_unreachable_is_a_clear_error(settings):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    configure(settings, port)
    with pytest.raises(MinecraftError, match="Can't reach"):
        minecraft.rcon(settings, "list")


@pytest.mark.parametrize("text", ["op Steve", "/op Steve", "execute as @a run op @s", "stop", "reload", ""])
def test_power_commands_are_refused_before_reaching_the_server(settings, monkeypatch, text):
    monkeypatch.setattr(minecraft, "rcon", lambda *a: pytest.fail("reached the server"))
    with pytest.raises(MinecraftError, match="isn't allowed"):
        minecraft.command(settings, text)


def test_allowed_command_goes_through(settings, monkeypatch):
    sent = []
    monkeypatch.setattr(minecraft, "is_running", lambda s: True)
    monkeypatch.setattr(minecraft, "rcon", lambda s, cmd: sent.append(cmd) or "Added Steve to the whitelist")
    assert minecraft.command(settings, "/whitelist add Steve") == "Added Steve to the whitelist"
    assert sent == ["whitelist add Steve"]


def test_launch_command_redirects_the_console_and_quotes_the_java_path():
    cmd = minecraft.launch_command(r"C:\Program Files\Java\jdk-25\bin\java.exe", 4, "paper-26.2-128.jar")
    assert cmd.startswith('cmd.exe /d /c ""C:\\Program Files\\Java\\jdk-25\\bin\\java.exe" -Xms4G -Xmx4G')
    assert cmd.endswith('-jar paper-26.2-128.jar --nogui > console.out 2>&1"')


def test_grant_op_checks_the_name_before_touching_the_server(settings, monkeypatch):
    monkeypatch.setattr(minecraft, "rcon", lambda *a: pytest.fail("reached the server"))
    with pytest.raises(MinecraftError, match="isn't a Minecraft name"):
        minecraft.grant_op(settings, "Steve; stop")


def test_grant_op_whitelists_then_ops(settings, monkeypatch):
    sent = []
    monkeypatch.setattr(minecraft, "is_running", lambda s: True)
    monkeypatch.setattr(minecraft, "rcon", lambda s, cmd: sent.append(cmd) or "ok")
    minecraft.grant_op(settings, "Steve")
    assert sent == ["whitelist add Steve", "op Steve"]


def test_status_before_setup_says_how(settings):
    assert "qm minecraft setup" in minecraft.status(settings)
