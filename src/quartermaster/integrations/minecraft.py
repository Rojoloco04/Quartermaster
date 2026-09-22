"""A Paper Minecraft server the owner starts, stops and runs commands on from
Discord. Friends reach it over Tailscale; nothing is port-forwarded.

The server lives outside both repos (the per-user data dir, or
``minecraft.dir``). ``qm minecraft setup`` downloads the newest *stable* Paper
build (checksum-verified), finds a Java new enough for it, and writes a
whitelisted ``server.properties`` with RCON on a random password. Everything
after that goes through RCON: the model never gets a raw console, only
``ALLOWED_COMMANDS``, so an email the owner agent read can't op someone or run
``execute``.

The server process is started detached and broken away from the caller's job:
the tool runs in an MCP server that dies when the turn ends, and ``qm quit``
must not take a world down mid-save.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import socket
import struct
import subprocess
import time
from pathlib import Path

import httpx

from ..config import Settings

PAPER_API = "https://fill.papermc.io/v3/projects/paper"
USER_AGENT = "quartermaster (self-hosted personal server manager)"
GAME_PORT, RCON_PORT = 25565, 25575
STATE_FILE, PID_FILE = "qm-server.json", "qm-server.pid"
STOP_WAIT_SECONDS = 60

# Every RCON call (the /servers page polls status every 10s) logs a connect and
# a disconnect. Hidden in the page's console; still in the file.
LOG_NOISE = re.compile(r"Thread RCON Client /127\.0\.0\.1 (started|shutting down)")
EULA_URL = "https://aka.ms/MinecraftEULA"

# First word of any command the model may send. Everything that grants power
# (op, deop, execute, function, reload, datapack) or ends the server (stop,
# which has its own tool) is left out on purpose.
ALLOWED_COMMANDS = frozenset({
    "list", "say", "tell", "msg", "whitelist", "kick", "ban", "pardon", "banlist",
    "time", "weather", "difficulty", "gamemode", "gamerule", "tp", "give", "seed",
    "save-all", "tps", "mspt",
})

# Paper's own recommended flags (Aikar's), as the PaperMC API lists them.
JVM_FLAGS = [
    "-XX:+AlwaysPreTouch", "-XX:+DisableExplicitGC", "-XX:+ParallelRefProcEnabled",
    "-XX:+PerfDisableSharedMem", "-XX:+UnlockExperimentalVMOptions", "-XX:+UseG1GC",
    "-XX:G1HeapRegionSize=8M", "-XX:G1HeapWastePercent=5", "-XX:G1MaxNewSizePercent=40",
    "-XX:G1MixedGCCountTarget=4", "-XX:G1MixedGCLiveThresholdPercent=90", "-XX:G1NewSizePercent=30",
    "-XX:G1RSetUpdatingPauseTimePercent=5", "-XX:G1ReservePercent=20",
    "-XX:InitiatingHeapOccupancyPercent=15", "-XX:MaxGCPauseMillis=200",
    "-XX:MaxTenuringThreshold=1", "-XX:SurvivorRatio=32",
]


class MinecraftError(RuntimeError):
    pass


class RconClosed(MinecraftError):
    pass


def server_dir(settings: Settings) -> Path:
    configured = (settings.prefs.get("minecraft") or {}).get("dir") or ""
    if configured:
        return Path(configured)
    from platformdirs import user_data_path

    return user_data_path("quartermaster", appauthor=False) / "minecraft"


def _memory_gb(settings: Settings) -> int:
    return int((settings.prefs.get("minecraft") or {}).get("memory_gb", 4))


# --- Setup ---------------------------------------------------------------------


def newest_stable(versions: dict[str, list[str]], builds_of) -> tuple[str, dict]:
    """The newest version with a STABLE build, and that build. ``versions`` is
    the API's family -> versions map (newest first); ``builds_of(version)``
    returns that version's builds, newest first. Pure given builds_of."""
    for family in versions.values():
        for version in family:
            if not re.fullmatch(r"[\d.]+", version):
                continue  # -rc / -pre
            for build in builds_of(version):
                if build.get("channel") == "STABLE":
                    return version, build
    raise MinecraftError("PaperMC lists no stable build")


def java_major(version_output: str) -> int | None:
    """The major version from ``java -version``'s output: 'version "25.0.4"'
    -> 25, the old '"1.8.0_392"' style -> 8."""
    m = re.search(r'version "(\d+)(?:\.(\d+))?', version_output)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major


def _java_candidates() -> list[Path]:
    found = []
    for root in (Path("C:/Program Files/Microsoft"), Path("C:/Program Files/Eclipse Adoptium"),
                 Path("C:/Program Files/Java"), Path("C:/Program Files/Zulu")):
        found += sorted(root.glob("*/bin/java.exe"), reverse=True)
    on_path = shutil.which("java")
    if on_path:
        found.append(Path(on_path))
    return found


def find_java(minimum: int) -> Path:
    seen = []
    for exe in _java_candidates():
        out = subprocess.run([str(exe), "-version"], capture_output=True, text=True)
        major = java_major(out.stderr + out.stdout)
        seen.append(f"{exe} ({major})")
        if major is not None and major >= minimum:
            return exe
    raise MinecraftError(
        f"Paper needs Java {minimum}+; found {', '.join(seen) or 'none'}. "
        f"Install it with: winget install Microsoft.OpenJDK.{minimum}"
    )


def default_properties(rcon_password: str) -> dict[str, str]:
    return {
        "motd": "Quartermaster",
        "server-port": str(GAME_PORT),
        "white-list": "true",
        "enforce-whitelist": "true",
        "online-mode": "true",
        "enable-rcon": "true",
        "rcon.port": str(RCON_PORT),
        "rcon.password": rcon_password,
        "broadcast-rcon-to-ops": "false",
    }


def read_properties(path: Path) -> dict[str, str]:
    props = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                props[key.strip()] = value.strip()
    return props


def write_properties(path: Path, props: dict[str, str]) -> None:
    path.write_text("".join(f"{k}={v}\n" for k, v in props.items()), encoding="utf-8")


def setup(settings: Settings, *, accept_eula: bool) -> str:
    """Download (or update) Paper and prepare the server folder. Safe to re-run:
    an existing world and server.properties are kept, only missing RCON and
    whitelist settings are filled in."""
    if not accept_eula:
        raise MinecraftError(f"Running a server means agreeing to Mojang's EULA ({EULA_URL}). "
                             "Read it, then re-run with --accept-eula.")
    if is_running(settings):
        raise MinecraftError("Stop the server before updating it.")
    folder = server_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as client:
        versions = client.get(PAPER_API).raise_for_status().json()["versions"]
        version, build = newest_stable(
            versions, lambda v: client.get(f"{PAPER_API}/versions/{v}/builds").raise_for_status().json()
        )
        info = client.get(f"{PAPER_API}/versions/{version}").raise_for_status().json()
        java = find_java(int(info["version"]["java"]["version"]["minimum"]))

        download = build["downloads"]["server:default"]
        jar = folder / download["name"]
        if not jar.exists():
            data = client.get(download["url"]).raise_for_status().content
            if hashlib.sha256(data).hexdigest() != download["checksums"]["sha256"]:
                raise MinecraftError(f"{download['name']} failed its checksum; nothing was changed")
            jar.write_bytes(data)
    for old in folder.glob("paper-*.jar"):
        if old != jar:
            old.unlink()

    (folder / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    props_path = folder / "server.properties"
    props = read_properties(props_path)
    for key, value in default_properties(secrets.token_urlsafe(24)).items():
        props.setdefault(key, value)
    props["enable-rcon"] = "true"  # the bot can't manage it without
    write_properties(props_path, props)
    (folder / STATE_FILE).write_text(
        json.dumps({"java": str(java), "jar": jar.name, "version": version, "build": build["id"]}, indent=2),
        encoding="utf-8",
    )
    return f"Paper {version} build {build['id']} ready in {folder} (Java: {java})."


# --- RCON ----------------------------------------------------------------------


def _packet(request_id: int, kind: int, body: str) -> bytes:
    payload = struct.pack("<ii", request_id, kind) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def _read_packet(sock: socket.socket) -> tuple[int, int, str]:
    def exactly(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RconClosed("RCON connection closed")
            buf += chunk
        return buf

    (length,) = struct.unpack("<i", exactly(4))
    data = exactly(length)
    request_id, kind = struct.unpack("<ii", data[:8])
    return request_id, kind, data[8:-2].decode("utf-8", errors="replace")


def rcon(settings: Settings, command: str, timeout: float = 5) -> str:
    props = read_properties(server_dir(settings) / "server.properties")
    password = props.get("rcon.password")
    if not password:
        raise MinecraftError("RCON isn't set up. Run: qm minecraft setup --accept-eula")
    port = int(props.get("rcon.port", RCON_PORT))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(_packet(1, 3, password))
            request_id, _, _ = _read_packet(sock)
            if request_id == -1:
                raise MinecraftError("RCON refused the password in server.properties")
            sock.sendall(_packet(2, 2, command))
            _, _, body = _read_packet(sock)
            return re.sub(r"§.", "", body)  # colour codes
    except OSError as exc:
        raise MinecraftError(f"Can't reach the server's RCON ({exc})") from exc


# --- Process -------------------------------------------------------------------


def log_path(settings: Settings) -> Path:
    """The console of the current (or last) run: everything Paper prints, plus
    a JVM that dies before logging starts. Truncated at each start."""
    return server_dir(settings) / "console.out"


def _pid(settings: Settings) -> int | None:
    try:
        return int((server_dir(settings) / PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    """The recorded pid is the cmd.exe that runs java and redirects its output."""
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout.lower()
    return '"cmd.exe"' in out or '"java.exe"' in out


def launch_command(java: str, mem: int, jar: str) -> str:
    """cmd.exe running the server with its output in console.out. Pure."""
    args = subprocess.list2cmdline([java, f"-Xms{mem}G", f"-Xmx{mem}G", *JVM_FLAGS, "-jar", jar, "--nogui"])
    return f'cmd.exe /d /c "{args} > console.out 2>&1"'


def _launch_detached(cmdline: str, cwd: Path) -> int:
    from .. import procs

    try:
        return procs.launch_outside_jobs(cmdline, cwd)
    except RuntimeError as exc:
        raise MinecraftError(str(exc)) from exc


def is_running(settings: Settings) -> bool:
    pid = _pid(settings)
    return pid is not None and _alive(pid)


def status(settings: Settings) -> str:
    folder = server_dir(settings)
    if not (folder / STATE_FILE).exists():
        return "Not set up yet. The owner runs: qm minecraft setup --accept-eula"
    state = json.loads((folder / STATE_FILE).read_text(encoding="utf-8"))
    if not is_running(settings):
        return f"Stopped (Paper {state['version']})."
    try:
        return f"Running (Paper {state['version']}). {rcon(settings, 'list')}"
    except MinecraftError:
        return f"Starting up (Paper {state['version']}); not accepting commands yet."


def start(settings: Settings) -> str:
    folder = server_dir(settings)
    if not (folder / STATE_FILE).exists():
        raise MinecraftError("Not set up yet. The owner runs: qm minecraft setup --accept-eula")
    if is_running(settings):
        return status(settings)
    state = json.loads((folder / STATE_FILE).read_text(encoding="utf-8"))
    mem = _memory_gb(settings)
    pid = _launch_detached(launch_command(state["java"], mem, state["jar"]), folder)
    (folder / PID_FILE).write_text(str(pid))
    time.sleep(3)
    if not _alive(pid):
        tail = (folder / "console.out").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-5:]
        raise MinecraftError("The server exited straight away:\n" + "\n".join(tail))
    return (f"Starting Paper {state['version']} with {mem}GB; it takes about a minute before anyone "
            f"can join (port {GAME_PORT}).")


def stop(settings: Settings) -> str:
    if not is_running(settings):
        return "It isn't running."
    try:
        rcon(settings, "stop")
    except RconClosed:
        pass  # it can hang up before answering: it's already shutting down
    except MinecraftError as exc:
        raise MinecraftError(f"Couldn't ask it to stop ({exc}). If it's still starting, try again in a minute.") from exc
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while is_running(settings) and time.monotonic() < deadline:
        time.sleep(1)
    if is_running(settings):
        return f"Asked it to stop; it's still saving after {STOP_WAIT_SECONDS}s. Check again shortly."
    (server_dir(settings) / PID_FILE).unlink(missing_ok=True)
    return "Stopped; the world is saved."


def online_players(list_output: str) -> list[str]:
    """Names from the `list` reply: 'There are 2 of a max of 20 players online: Steve, Alex'."""
    _, _, names = list_output.partition(":")
    return [n.strip() for n in names.split(",") if n.strip()]


# --- Discord links -------------------------------------------------------------
#
# A Discord member may control the server from a guild channel only if their
# linked Minecraft name is on the server's op list. A link is made by proving
# both accounts at once: the bot whispers a code to the player in-game and the
# member posts it back in Discord. The file is the owner's to hand-edit (in
# /settings) and on agent._PROTECTED, so the owner agent can't add one.

NAME = re.compile(r"[A-Za-z0-9_]{3,16}")
LINKS_HEADER = ("# Minecraft links\n\nDiscord user id: Minecraft name, one per line. Added when someone proves\n"
                "both accounts (a code whispered in-game); edit by hand to remove one.\n\n")


def links_path(settings: Settings) -> Path:
    return settings.system_dir / "minecraft-links.md"


def read_links(path: Path) -> dict[int, str]:
    links = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.fullmatch(r"\s*-?\s*(\d{15,21})\s*:\s*([A-Za-z0-9_]{3,16})\s*", line)
            if m:
                links[int(m.group(1))] = m.group(2)
    return links


def add_link(path: Path, discord_id: int, name: str) -> None:
    """One Minecraft name per Discord user and vice versa: a new proof replaces
    either side's old link."""
    links = {d: n for d, n in read_links(path).items() if d != discord_id and n.lower() != name.lower()}
    links[discord_id] = name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(LINKS_HEADER + "".join(f"- {d}: {n}\n" for d, n in links.items()), encoding="utf-8")


def op_names(settings: Settings) -> set[str]:
    try:
        ops = json.loads((server_dir(settings) / "ops.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(o.get("name", "")).lower() for o in ops if o.get("name")}


def may_control(settings: Settings, discord_id: int) -> tuple[bool, str]:
    """(allowed, why not). The owner always may; anyone else needs a linked
    name that is on the op list right now."""
    if settings.discord_owner_id and discord_id == settings.discord_owner_id:
        return True, ""
    name = read_links(links_path(settings)).get(discord_id)
    if name is None:
        return False, ("Only the server's ops can do that, and your Discord isn't linked to a Minecraft "
                       "name. Join the server and say \"link me to <your name>\".")
    if name.lower() not in op_names(settings):
        return False, f"Only the server's ops can do that, and {name} isn't on the op list."
    return True, ""


class LinkCodes:
    """Pending link codes, in memory (a restart just means asking again).
    Each is bound to the Discord user who asked, so a code seen in a channel is
    useless to anyone else, and expires after ``TTL``."""

    TTL = 600
    MAX_TRIES = 5

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._pending: dict[int, tuple[str, str, float, int]] = {}

    def issue(self, discord_id: int, name: str) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._pending[discord_id] = (code, name, self._clock() + self.TTL, 0)
        return code

    def redeem(self, discord_id: int, code: str) -> str | None:
        """The Minecraft name if ``code`` is this user's live code, else None."""
        entry = self._pending.get(discord_id)
        if entry is None:
            return None
        expected, name, expires, tries = entry
        if self._clock() > expires or tries >= self.MAX_TRIES:
            self._pending.pop(discord_id, None)
            return None
        if not secrets.compare_digest(code.strip(), expected):
            self._pending[discord_id] = (expected, name, expires, tries + 1)
            return None
        self._pending.pop(discord_id, None)
        return name


def grant_op(settings: Settings, name: str) -> str:
    """Whitelist and op a player. Terminal only (``qm minecraft op``): no chat
    tool reaches this, which is what keeps ``op`` out of ALLOWED_COMMANDS."""
    if not NAME.fullmatch(name):
        raise MinecraftError(f"{name!r} isn't a Minecraft name.")
    if not is_running(settings):
        raise MinecraftError("Start the server first; ops are granted through it.")
    return "\n".join(rcon(settings, f"{verb} {name}") for verb in ("whitelist add", "op"))


def command(settings: Settings, text: str) -> str:
    text = text.strip().removeprefix("/")
    verb = text.split(" ", 1)[0].lower() if text else ""
    if verb not in ALLOWED_COMMANDS:
        raise MinecraftError(f"'{verb or text}' isn't allowed from chat. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}.")
    if not is_running(settings):
        raise MinecraftError("The server isn't running.")
    return rcon(settings, text) or "Done (no output)."
