"""The Starlette app: routes, the Host check, the token gate and CSRF, and
``serve`` for ``qm web``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from dataclasses import replace

from .. import agent, chat
from ..config import REPO_ROOT, Settings
from ..ops import game_backup
from . import brain
from .chat_view import chat_page
from .dashboard_view import dashboard, read_log_from, TAIL_BYTES
from .layout import _e, markdown_to_html, NAV, page
from .servers_view import servers_page
from .settings_view import (
    EDIT_CSS,
    EDIT_JS,
    editable_path,
    EditRefused,
    file_hash,
    render_file,
    save_file,
    set_pref,
    settings_page,
)


log = logging.getLogger(__name__)


# --- App -----------------------------------------------------------------------


LOCAL_HOSTS = ("127.0.0.1", "localhost")


def build_app(settings: Settings, token: str | None = None, hosts: tuple[str, ...] = LOCAL_HOSTS):
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
    from starlette.routing import Route

    # Per run: pages carry it, saves must send it back as a header.
    csrf = secrets.token_urlsafe(32)

    def authorised(request: Request) -> bool:
        if token is None:
            return True
        given = request.query_params.get("token") or request.cookies.get("qm_token") or ""
        return secrets.compare_digest(given, token)

    def guarded(handler):
        async def wrapper(request: Request):
            if not authorised(request):
                return PlainTextResponse("Unauthorised. Open /?token=<QM_WEB_TOKEN> once.", status_code=401)
            response = await handler(request)
            if token is not None and request.query_params.get("token"):
                response = RedirectResponse(request.url.path)
                response.set_cookie("qm_token", token, httponly=True, samesite="strict", max_age=30 * 86400)
            return response
        return wrapper

    async def index(request: Request):
        return HTMLResponse(dashboard(settings))

    async def api_log(request: Request):
        pos, text = read_log_from(settings.log_path, int(request.query_params.get("pos", -20000)))
        return JSONResponse({"pos": pos, "text": text})

    async def digest(request: Request):
        name = request.path_params["name"]
        path = settings.digests_dir / f"{name}.md"
        if not re.fullmatch(r"[\w-]+", name) or not path.exists():
            return PlainTextResponse("No such digest.", status_code=404)
        return HTMLResponse(page(f"Digest {name}", f"<section><h2>Digest {_e(name)}</h2><pre>{_e(path.read_text('utf-8'))}</pre></section>"))

    async def guide(request: Request):
        path = REPO_ROOT / "docs" / "GUIDE.md"
        text = path.read_text("utf-8") if path.exists() else "# Guide\n\nNot written yet."
        return HTMLResponse(page("Guide", f"<section>{markdown_to_html(text)}</section>"))

    async def architecture(request: Request):
        # A standalone page in docs/, so it also reads fine opened from the repo.
        path = REPO_ROOT / "docs" / "architecture.html"
        if not path.exists():
            return PlainTextResponse("docs/architecture.html is missing.", status_code=404)
        return HTMLResponse(re.sub(r"<nav>.*?</nav>", lambda _: NAV, path.read_text("utf-8"), count=1, flags=re.S))

    def graph() -> dict:
        _, log_text = read_log_from(settings.log_path, -TAIL_BYTES)
        return brain.build(settings.vault, log_text)

    async def brain_page(request: Request):
        return HTMLResponse(page("Brain", brain.BODY, EDIT_JS + brain.JS, EDIT_CSS + brain.CSS, csrf))

    async def settings_view(request: Request):
        return HTMLResponse(settings_page(settings, csrf))

    async def api_file_save(request: Request):
        if not secrets.compare_digest(request.headers.get("x-qm-csrf", ""), csrf):
            return JSONResponse({"error": "Missing or stale page token. Reload the page."}, status_code=403)
        try:
            body = await request.json()
            rel, text = str(body["path"]), str(body["text"])
            new_hash = save_file(settings.vault, rel, text, str(body.get("hash", "")))
        except EditRefused as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "Malformed save."}, status_code=400)
        saved = (settings.vault / rel).read_text("utf-8")
        return JSONResponse({"hash": new_hash, "html": render_file(rel, saved)})

    async def api_pref_save(request: Request):
        if not secrets.compare_digest(request.headers.get("x-qm-csrf", ""), csrf):
            return JSONResponse({"error": "Missing or stale page token. Reload the page."}, status_code=403)
        try:
            body = await request.json()
            new_hash = set_pref(settings.vault, str(body["key"]), str(body["value"]), str(body.get("hash", "")))
        except EditRefused as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "Malformed save."}, status_code=400)
        return JSONResponse({"hash": new_hash})

    async def chat_view(request: Request):
        return HTMLResponse(chat_page(settings, csrf))

    # --- Game servers: status, start/stop, console. Each call can block (RCON,
    # tasklist, a 60s stop), so they run off the event loop.
    from ..games import GAMES, clean_console

    async def game_status(game) -> str:
        try:
            return await asyncio.to_thread(game.module.status, settings)
        except RuntimeError as exc:
            return str(exc)

    async def servers(request: Request):
        key = request.path_params.get("game") or next(iter(GAMES))
        if key not in GAMES:
            return PlainTextResponse("No such server.", status_code=404)
        game = GAMES[key]
        backup = ""
        if hasattr(game.module, "backup_sources"):  # the backup drive may need to spin up
            backup = await asyncio.to_thread(game_backup.last_backup, settings, key)
        return HTMLResponse(servers_page(key, await game_status(game), game.module.log_path(settings), csrf, backup))

    async def api_server_status(request: Request):
        game = GAMES.get(request.path_params["game"])
        if game is None:
            return JSONResponse({"error": "No such server."}, status_code=404)
        return JSONResponse({"status": await game_status(game)})

    async def api_server_log(request: Request):
        game = GAMES.get(request.path_params["game"])
        if game is None:
            return JSONResponse({"error": "No such server."}, status_code=404)
        asked = int(request.query_params.get("pos", -20000))
        pos, text = read_log_from(game.module.log_path(settings), asked, whole_lines=True)
        # The console is truncated at each start: tell the page to clear.
        return JSONResponse({"pos": pos, "text": clean_console(game, text), "reset": asked > 0 and pos < asked})

    async def api_server_action(request: Request):
        if not secrets.compare_digest(request.headers.get("x-qm-csrf", ""), csrf):
            return JSONResponse({"error": "Missing or stale page token. Reload the page."}, status_code=403)
        game, action = GAMES.get(request.path_params["game"]), request.path_params["action"]
        if game is None or action not in ("start", "stop"):
            return JSONResponse({"error": "No such server or action."}, status_code=404)
        try:
            result = await asyncio.to_thread(getattr(game.module, action), settings)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        log.info("web: %s %s: %s", action, game.key, result)
        return JSONResponse({"result": result})

    # The web chat's own turn, as the bot keeps its own: "stop" here cancels
    # this one. chat.TurnLock keeps it from overlapping a Discord turn.
    owner = agent.owner_profile(settings)
    turn: dict = {"task": None, "fresh": False}

    def events(queue: asyncio.Queue):
        async def stream():
            while (event := await queue.get()) is not None:
                yield f"data: {json.dumps(event)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    def note(text: str):
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"kind": "note", "text": text})
        queue.put_nowait(None)
        return events(queue)

    async def api_chat(request: Request):
        if not secrets.compare_digest(request.headers.get("x-qm-csrf", ""), csrf):
            return JSONResponse({"error": "Missing or stale page token. Reload the page."}, status_code=403)
        try:
            text = str((await request.json())["text"]).strip()
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "Malformed message."}, status_code=400)
        if not text:
            return JSONResponse({"error": "Empty message."}, status_code=400)

        running = turn["task"] is not None and not turn["task"].done()
        control = chat.session_control(text)
        if control == "stop":
            if running:
                turn["task"].cancel()
                return note("Stopping.")
            return note("Nothing is running here.")
        if control == "new":
            turn["fresh"] = True
            return note(chat.FRESH_NOTE)
        if running:
            return note('Still working on the last one. Say "stop" to cancel it.')
        lock = chat.TurnLock(chat.lock_path(settings))
        if not lock.acquire():
            return note("Busy with a message from Discord. Try again when it's answered.")

        profile = chat.continue_or_fresh(settings, owner)
        if turn["fresh"]:
            profile, turn["fresh"] = replace(owner, share_session=False), False
        queue: asyncio.Queue = asyncio.Queue()
        sent = False

        async def progress(kind: str, payload: object) -> None:
            nonlocal sent
            if kind == "text" and str(payload).strip():
                queue.put_nowait({"kind": "text", "html": markdown_to_html(str(payload).strip())})
                sent = True
            elif kind == "tool":
                name, tool_input = payload  # type: ignore[misc]
                queue.put_nowait({"kind": "tool", "text": chat.describe_tool(name, tool_input or {})})

        async def run() -> None:
            # A task of its own, not the response's: closing the tab mid-turn
            # leaves the turn to finish, and its reply lands in the transcript.
            try:
                reply = await agent.ask(text, profile, settings.claude_cli, on_progress=progress)
                if reply.error:
                    queue.put_nowait({"kind": "error", "text": reply.error})
                elif not sent:
                    queue.put_nowait({"kind": "error", "text": "I finished but produced no reply. That's a bug."})
            except asyncio.CancelledError:
                log.info("web chat turn stopped by the owner")
                queue.put_nowait({"kind": "note", "text": "⏹️ Stopped."})
            finally:
                lock.release()
                queue.put_nowait(None)

        queue.put_nowait({"kind": "start"})
        turn["task"] = asyncio.create_task(run())
        return events(queue)

    async def api_brain(request: Request):
        return JSONResponse(graph())

    async def api_brain_note(request: Request):
        note_id = request.query_params.get("id", "")
        path = brain.note_path(settings.vault, note_id)
        if path is None:
            return JSONResponse({"error": "No such note."}, status_code=404)
        meta, body = brain.frontmatter(path.read_text("utf-8", errors="replace"))
        root = settings.vault.resolve()

        def resolve(target: str) -> str | None:
            candidate = (path.parent / target.split("#")[0]).resolve()
            if root not in candidate.parents:
                return None
            rel = candidate.relative_to(root).as_posix()
            return rel if brain.note_path(settings.vault, rel) else None

        g = graph()
        node = next((n for n in g["nodes"] if n["id"] == note_id), {})
        out, back = brain.neighbours(g, note_id)
        url = meta.get("url", "")
        editable = editable_path(settings.vault, note_id) is not None
        body = re.sub(r"\A\s*# .*\n?", "", body)  # the panel header already shows the title
        return JSONResponse({**node, "html": markdown_to_html(body, resolve), "out": out, "back": back,
                             "url": url if url.startswith("https://") else "", "editable": editable,
                             **({"raw": path.read_text("utf-8"), "hash": file_hash(path)} if editable else {})})

    return Starlette(middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=list(hosts))], routes=[
        Route("/", guarded(index)),
        Route("/chat", guarded(chat_view)),
        Route("/api/chat", guarded(api_chat), methods=["POST"]),
        Route("/settings", guarded(settings_view)),
        Route("/architecture", guarded(architecture)),
        Route("/api/file", guarded(api_file_save), methods=["POST"]),
        Route("/api/pref", guarded(api_pref_save), methods=["POST"]),
        Route("/brain", guarded(brain_page)),
        Route("/api/brain", guarded(api_brain)),
        Route("/api/brain/note", guarded(api_brain_note)),
        Route("/api/log", guarded(api_log)),
        Route("/servers", guarded(servers)),
        Route("/servers/{game}", guarded(servers)),
        Route("/api/servers/{game}/status", guarded(api_server_status)),
        Route("/api/servers/{game}/log", guarded(api_server_log)),
        Route("/api/servers/{game}/{action}", guarded(api_server_action), methods=["POST"]),
        Route("/digest/{name}", guarded(digest)),
        Route("/guide", guarded(guide)),
    ])


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8766) -> int:
    import uvicorn

    token = os.getenv("QM_WEB_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise RuntimeError(f"Refusing to serve on {host} without QM_WEB_TOKEN set: the log holds DMs and email snippets.")
    hosts = LOCAL_HOSTS if host in LOCAL_HOSTS else (*LOCAL_HOSTS, host)
    uvicorn.run(build_app(settings, token, hosts), host=host, port=port, log_level="warning")
    return 0
