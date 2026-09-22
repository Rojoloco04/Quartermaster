"""A Satisfactory dedicated server the owner starts and stops from Discord or
``qm web``. Friends reach it over Tailscale, like the Minecraft server.

The install lives outside both repos (the per-user data dir, or
``satisfactory.dir``): ``qm satisfactory setup`` fetches SteamCMD and installs
(or updates) the server with it. The game keeps its saves where every Windows
server does, ``%LOCALAPPDATA%\\FactoryGame\\Saved\\SaveGames\\server`` (beside
the owner's own client saves, which it never touches).

Everything after launch goes through the server's HTTPS API on 127.0.0.1. The
first contact claims the server with a random admin password kept in
``qm-server.json``: an unclaimed server can be claimed by whoever reaches it
first. There is no chat command tool: the API's ``RunCommand`` is a full admin
console, and nothing here needs one.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

import httpx

from ..config import Settings

APP_ID = "1690800"
STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
GAME_PORT, RELIABLE_PORT = 7777, 8888
API_URL = f"https://127.0.0.1:{GAME_PORT}/api/v1"
STATE_FILE, PID_FILE = "qm-server.json", "qm-server.pid"
SERVER_EXE = Path("Engine/Binaries/Win64/FactoryServer-Win64-Shipping-Cmd.exe")
STOP_WAIT_SECONDS = 90

# Startup chatter, about 70% of a run's log (API calls log nothing, so the
# page's status poll adds none). Hidden in the console box; still in the file.
LOG_NOISE = re.compile(r"\]Log(StaticMesh|CSLocTools|PluginManager):")


class SatisfactoryError(RuntimeError):
    pass


class ApiError(SatisfactoryError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"The server refused: {code}" + (f" ({message})" if message else ""))
        self.code = code


class Unreachable(SatisfactoryError):
    """Nothing answers on the API port: still starting, or gone."""


def server_dir(settings: Settings) -> Path:
    configured = (settings.prefs.get("satisfactory") or {}).get("dir") or ""
    if configured:
        return Path(configured)
    from platformdirs import user_data_path

    return user_data_path("quartermaster", appauthor=False) / "satisfactory"


def install_dir(settings: Settings) -> Path:
    return server_dir(settings) / "server"


def save_root() -> Path:
    """Where the Windows server keeps saves (``server``) and blueprints
    (``blueprints/<session>``). Fixed by the game."""
    return Path(os.environ["LOCALAPPDATA"]) / "FactoryGame" / "Saved" / "SaveGames"


def _state(settings: Settings) -> dict:
    try:
        return json.loads((server_dir(settings) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _installed(settings: Settings) -> bool:
    return (install_dir(settings) / SERVER_EXE).exists() and bool(_state(settings).get("admin_password"))


NOT_SET_UP = "Not set up yet. The owner runs: qm satisfactory setup"


# --- Setup ---------------------------------------------------------------------


def _steamcmd(settings: Settings) -> Path:
    exe = server_dir(settings) / "steamcmd" / "steamcmd.exe"
    if not exe.exists():
        data = httpx.get(STEAMCMD_URL, timeout=60, follow_redirects=True).raise_for_status().content
        zipfile.ZipFile(io.BytesIO(data)).extractall(exe.parent)
    return exe


def setup(settings: Settings) -> str:
    """Install or update the server through SteamCMD (anonymous login; its
    progress prints to the terminal). Safe to re-run: saves live elsewhere and
    the admin password is kept."""
    if is_running(settings):
        raise SatisfactoryError("Stop the server before updating it.")
    folder = server_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    steam = _steamcmd(settings)
    # A fresh SteamCMD updates itself and then fails the install with "Missing
    # configuration" (exit 7); the second run works.
    for _ in range(2):
        code = subprocess.run([str(steam), "+force_install_dir", str(install_dir(settings)), "+login",
                               "anonymous", "+app_update", APP_ID, "validate", "+quit"]).returncode
        if code == 0:
            break
    if code != 0 or not (install_dir(settings) / SERVER_EXE).exists():
        raise SatisfactoryError(f"SteamCMD didn't install the server (exit {code}); its output is above.")
    state = _state(settings)
    state.setdefault("admin_password", secrets.token_urlsafe(18))
    (folder / STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")
    return (f"Satisfactory server ready in {install_dir(settings)}. Start it with: qm satisfactory start\n"
            f"Its admin password (for the in-game Server Manager) is in {folder / STATE_FILE}.")


# --- HTTPS API -----------------------------------------------------------------


def api(function: str, data: dict | None = None, token: str | None = None, timeout: float = 10) -> dict:
    """One API call. The server's certificate is self-signed and this only ever
    talks to 127.0.0.1, so it isn't verified."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = httpx.post(API_URL, json={"function": function, "data": data or {}}, headers=headers,
                       verify=False, timeout=timeout)
    except httpx.HTTPError as exc:
        raise Unreachable(f"Can't reach the server's API ({exc})") from exc
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {}
    if r.status_code >= 400 or "errorCode" in body:
        raise ApiError(body.get("errorCode", f"HTTP {r.status_code}"), body.get("errorMessage", ""))
    return body.get("data", {})


def field(data: dict, key: str, default=None):
    """``data[key]`` ignoring case: the API's docs spell fields both ways."""
    key = key.lower()
    return next((v for k, v in data.items() if k.lower() == key), default)


def _token_of(data: dict) -> str:
    return field(data, "authenticationToken", "")


def admin_token(settings: Settings) -> str:
    """An admin token. A server nobody has claimed yet is claimed on the spot
    with our password, closing the window where anyone who reaches it could."""
    password = _state(settings).get("admin_password")
    if not password:
        raise SatisfactoryError(NOT_SET_UP)
    try:
        return _token_of(api("PasswordLogin", {"MinimumPrivilegeLevel": "Administrator", "Password": password}))
    except ApiError as refused:
        try:
            initial = _token_of(api("PasswordlessLogin", {"MinimumPrivilegeLevel": "InitialAdmin"}))
        except ApiError:
            raise SatisfactoryError(f"Can't log in as admin: {refused}. If it was claimed in-game, put that "
                                    f"admin password in {server_dir(settings) / STATE_FILE}.") from refused
        name = (settings.prefs.get("satisfactory") or {}).get("server_name") or "Quartermaster"
        return _token_of(api("ClaimServer", {"ServerName": name, "AdminPassword": password}, initial))


# --- Process -------------------------------------------------------------------


def log_path(settings: Settings) -> Path:
    """The current run's log. The server rotates it to a backup at each start."""
    return install_dir(settings) / "FactoryGame" / "Saved" / "Logs" / "FactoryGame.log"


def _pid(settings: Settings) -> int | None:
    try:
        return int((server_dir(settings) / PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout.lower()
    return f'"{SERVER_EXE.name.lower()}"' in out


def is_running(settings: Settings) -> bool:
    pid = _pid(settings)
    return pid is not None and _alive(pid)


def launch_command(exe: Path) -> str:
    """Pure. ``-log`` so the log file is written; ``-unattended`` so no dialog waits."""
    return subprocess.list2cmdline([str(exe), "FactoryGame", "-log", "-unattended"])


def _launch_detached(cmdline: str, cwd: Path) -> int:
    from .. import procs

    try:
        return procs.launch_outside_jobs(cmdline, cwd)
    except RuntimeError as exc:
        raise SatisfactoryError(str(exc)) from exc


def describe_state(s: dict) -> str:
    """The status line from ``QueryServerState``'s ``serverGameState``. Pure."""
    if not s.get("isGameRunning"):
        return "Running, but no save is loaded (the owner runs: qm satisfactory import <save zip>)."
    players = f"{s.get('numConnectedPlayers', 0)} of {s.get('playerLimit', '?')} players online"
    paused = ", paused" if s.get("isGamePaused") else ""
    hours = int(s.get("totalGameDuration", 0)) // 3600
    return (f"Running {s.get('activeSessionName') or 'a session'}: {players}{paused}; tier "
            f"{s.get('techTier', '?')}, {hours}h played, {float(s.get('averageTickRate', 0)):.0f} ticks/s.")


def status(settings: Settings) -> str:
    if not _installed(settings):
        return NOT_SET_UP
    if not is_running(settings):
        return "Stopped."
    try:
        token = admin_token(settings)
        return describe_state(field(api("QueryServerState", token=token), "serverGameState", {}))
    except Unreachable:
        return "Starting up; not accepting connections yet (it takes a minute or two)."


def start(settings: Settings) -> str:
    if not _installed(settings):
        raise SatisfactoryError(NOT_SET_UP)
    if is_running(settings):
        return status(settings)
    pid = _launch_detached(launch_command(install_dir(settings) / SERVER_EXE), install_dir(settings))
    (server_dir(settings) / PID_FILE).write_text(str(pid))
    time.sleep(3)
    if not _alive(pid):
        raise SatisfactoryError(f"The server exited straight away; see {log_path(settings)}.")
    return (f"Starting the Satisfactory server; it takes a minute or two before anyone can join "
            f"(port {GAME_PORT}).")


def save_name(session: str, now: float | None = None) -> str:
    """Named like the game's own manual saves (``Session_ddmmyy-HHMMSS``), so none overwrites another. Pure."""
    return f"{session}_{time.strftime('%d%m%y-%H%M%S', time.localtime(now))}"


def _save(settings: Settings, token: str) -> str | None:
    state = field(api("QueryServerState", token=token), "serverGameState", {})
    if not state.get("isGameRunning"):
        return None
    name = save_name(state.get("activeSessionName") or "session")
    api("SaveGame", {"SaveName": name}, token, timeout=120)
    return name


def save(settings: Settings) -> str:
    if not is_running(settings):
        raise SatisfactoryError("The server isn't running.")
    name = _save(settings, admin_token(settings))
    return f"Saved as {name}." if name else "No save is loaded, so there's nothing to save."


def stop(settings: Settings) -> str:
    """Save first (the API's Shutdown doesn't promise to), then shut down."""
    if not is_running(settings):
        return "It isn't running."
    try:
        token = admin_token(settings)
        saved = _save(settings, token)
        api("Shutdown", token=token)
    except SatisfactoryError as exc:
        raise SatisfactoryError(f"Couldn't shut it down cleanly ({exc}). If it's still starting, try again "
                                "in a minute.") from exc
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while is_running(settings) and time.monotonic() < deadline:
        time.sleep(1)
    if is_running(settings):
        return f"Asked it to shut down; still going after {STOP_WAIT_SECONDS}s. Check again shortly."
    (server_dir(settings) / PID_FILE).unlink(missing_ok=True)
    return f"Stopped; saved as {saved}." if saved else "Stopped (no save was loaded)."


# --- Importing a save ----------------------------------------------------------


def _extract(archive: Path, into: Path) -> None:
    """A .zip or .tar(.gz), and any archive inside it (a hosting panel's
    backup is often a tarball zipped once more)."""
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            t.extractall(into, filter="data")
    else:
        raise SatisfactoryError(f"{archive.name} isn't a zip or tar archive.")
    for inner in [p for p in into.rglob("*") if p.is_file() and p != archive]:
        if zipfile.is_zipfile(inner) or (inner.suffix in (".tar", ".gz", ".tgz") and tarfile.is_tarfile(inner)):
            _extract(inner, inner.parent)
            inner.unlink()


def import_files(source: Path, dest: Path) -> tuple[list[str], list[str]]:
    """Copy saves and blueprints found anywhere under ``source`` into the
    server's layout under ``dest`` (``save_root()``). Existing files are never
    overwritten. Returns (copied save names, skipped paths). The old host's
    ``ServerSettings.<port>.sav`` holds its own admin password and is left out."""
    saves, skipped = [], []
    for f in sorted(source.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix == ".sav" and not f.name.startswith(("ServerSettings", "ServerManager")):
            target = dest / "server" / f.name
        elif f.suffix in (".sbp", ".sbpcfg") and f.parent.parent.name == "blueprints":
            target = dest / "blueprints" / f.parent.name / f.name
        else:
            continue
        if target.exists():
            skipped.append(str(target.relative_to(dest)))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        if f.suffix == ".sav":
            saves.append(f.stem)
    return saves, skipped


def newest_save(sessions: list[dict], names: set[str]) -> tuple[str, str] | None:
    """(session, save) of the newest save among ``names``, from
    ``EnumerateSessions``. The header's date, not file times: a zip loses them. Pure."""
    found = [(field(h, "saveDateTime", ""), field(s, "sessionName", ""), field(h, "saveName"))
             for s in sessions for h in field(s, "saveHeaders", []) if field(h, "saveName") in names]
    if not found:
        return None
    _, session, name = max(found)
    return session, name


def import_save(settings: Settings, archive: Path) -> str:
    """Copy an old server's saves and blueprints in, load the newest save and
    make its session the one loaded on every start. Terminal only
    (``qm satisfactory import``). The server must be up: loading goes through it."""
    if not archive.is_file():
        raise SatisfactoryError(f"No file at {archive}.")
    if not is_running(settings):
        raise SatisfactoryError("Start the server first (qm satisfactory start) and give it a minute; "
                                "the save is loaded through it.")
    token = admin_token(settings)
    with tempfile.TemporaryDirectory() as tmp:
        _extract(archive, Path(tmp))
        saves, skipped = import_files(Path(tmp), save_root())
    names = set(saves) | {Path(p).stem for p in skipped if p.endswith(".sav")}
    picked = newest_save(field(api("EnumerateSessions", token=token), "sessions", []), names)
    if picked is None:
        raise SatisfactoryError(f"Copied {len(saves)} saves, but the server lists none of them "
                                f"({', '.join(sorted(names)) or 'no .sav files found'}).")
    session, name = picked
    api("SetAutoLoadSessionName", {"SessionName": session}, token)
    api("LoadGame", {"SaveName": name, "EnableAdvancedGameSettings": False}, token, timeout=120)
    lines = [f"Copied {len(saves)} saves into {save_root() / 'server'}.",
             f"Loading {name} (session {session}); it's the session loaded on every start from now on."]
    if skipped:
        lines.append("Already there, left alone: " + ", ".join(skipped))
    return "\n".join(lines)
