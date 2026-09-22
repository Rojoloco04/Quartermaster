"""Microsoft To Do, via Microsoft Graph.

One MSAL token cache per Microsoft account, each labelled by the owner
("personal", say). The Azure app is registered as a public client ("Mobile
and desktop applications", redirect URI ``http://localhost``) with tenant
``consumers`` - personal Microsoft accounts only, no organisation to admin-
consent. A public client has no secret to leak: PKCE secures the exchange
instead, which is also why there is no MICROSOFT_CLIENT_SECRET anywhere.

Tokens live in the per-user config directory, outside both the public code
repo and the vault repo, same as Google's. MSAL manages its own cache format
(``SerializableTokenCache``); we just persist the blob it hands back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Settings
from . import accounts as _acct

GRAPH = "https://graph.microsoft.com/v1.0"

# MSAL adds openid, profile and offline_access to every token request on its
# own - passing any of them here raises ValueError("reserved"). It still gets
# a refresh token without being asked; only the scope our own code cares
# about goes in this list.
SCOPES = ["Tasks.ReadWrite"]


class MicrosoftError(RuntimeError):
    pass


# --- Accounts and tokens ------------------------------------------------------


def token_path(settings: Settings, label: str) -> Path:
    return _acct.token_path(settings, "microsoft", label, MicrosoftError)


def accounts(settings: Settings) -> list[str]:
    return _acct.accounts(settings, "microsoft")


def resolve_account(settings: Settings, account: str | None) -> str:
    return _acct.resolve_one(settings, "microsoft", account, MicrosoftError)


def _cache(path: Path):
    import msal

    cache = msal.SerializableTokenCache()
    if path.exists():
        cache.deserialize(path.read_text("utf-8"))
    return cache


def _save_cache(path: Path, cache) -> None:
    # has_state_changed is false on a plain silent-refresh no-op; skip the
    # write rather than touch the file (and its mtime) for nothing.
    if cache.has_state_changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cache.serialize(), encoding="utf-8")


def _app(settings: Settings, cache):
    import msal

    settings.require("microsoft_client_id")
    authority = f"https://login.microsoftonline.com/{settings.microsoft_tenant_id}"
    return msal.PublicClientApplication(
        settings.microsoft_client_id, authority=authority, token_cache=cache
    )


def authorize(settings: Settings, label: str) -> str:
    """Run the browser consent flow for one account and save its token cache.

    Returns the signed-in username so the caller can confirm the right
    account was picked in the browser.
    """
    path = token_path(settings, label)
    cache = _cache(path)
    app = _app(settings, cache)
    result = app.acquire_token_interactive(SCOPES)
    _save_cache(path, cache)

    if "access_token" not in result:
        detail = (result.get("error_description") or result.get("error") or "no access token").splitlines()[0]
        raise MicrosoftError(f"No Microsoft To Do permission was granted, so nothing was saved. ({detail})")
    return result.get("id_token_claims", {}).get("preferred_username", "?")


def _access_token(settings: Settings, label: str) -> str:
    """A valid access token for one account, refreshing silently if needed."""
    path = _acct.existing_token(settings, "microsoft", label, MicrosoftError)
    cache = _cache(path)
    app = _app(settings, cache)
    account = next(iter(app.get_accounts()), None)
    if account is None:
        raise MicrosoftError(f"The {label!r} token is unusable. Run: qm auth microsoft {label}")

    result = app.acquire_token_silent(SCOPES, account=account)
    _save_cache(path, cache)
    if not result or "access_token" not in result:
        raise MicrosoftError(f"The {label!r} token was refused. Re-authorise with: qm auth microsoft {label}")
    return result["access_token"]


# --- Graph calls ----------------------------------------------------------------


def _raise_for_status(resp) -> None:
    if resp.status_code >= 400:
        try:
            reason = resp.json().get("error", {}).get("message")
        except ValueError:
            reason = None
        raise MicrosoftError(f"Microsoft To Do request failed: {reason or resp.text[:200] or resp.status_code}")


def _request(settings: Settings, label: str, method: str, path: str, **kwargs) -> dict:
    import httpx

    token = _access_token(settings, label)
    resp = httpx.request(
        method, f"{GRAPH}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=15, **kwargs
    )
    _raise_for_status(resp)
    return resp.json() if resp.content else {}


# --- Time -------------------------------------------------------------------------


def _iso(value: str) -> str:
    """A bare date becomes local midnight; Graph wants a dateTimeTimeZone pair."""
    value = value.strip()
    if len(value) == 10:
        return f"{value}T00:00:00"
    return value


def due_body(value: str) -> dict:
    return {"dateTime": _iso(value), "timeZone": "UTC"}


# --- Task lists -------------------------------------------------------------------


def list_task_lists(settings: Settings, account: str | None = None) -> str:
    label = resolve_account(settings, account)
    items = _request(settings, label, "GET", "/me/todo/lists").get("value", [])
    if not items:
        return "No task lists."
    return "\n".join(f"- {it.get('displayName')}  id={it.get('id')}" for it in items)


def _default_list_id(settings: Settings, label: str) -> str:
    items = _request(settings, label, "GET", "/me/todo/lists").get("value", [])
    for it in items:
        if it.get("wellknownListName") == "defaultList":
            return it["id"]
    if not items:
        raise MicrosoftError("That account has no task lists.")
    return items[0]["id"]


# --- Tasks --------------------------------------------------------------------


def format_task(task: dict) -> str:
    due = (task.get("dueDateTime") or {}).get("dateTime", "")
    due_part = f"  due {due[:10]}" if due else ""
    done = "x" if task.get("status") == "completed" else " "
    lines = [f"[{done}] {task.get('title')}{due_part}  (id {task.get('id')})"]
    notes = ((task.get("body") or {}).get("content") or "").strip()
    if notes:
        lines.append(f"    notes: {notes[:300]}")
    # Checklist steps: often where the real content of a task lives.
    for step in task.get("checklistItems") or []:
        lines.append(f"    [{'x' if step.get('isChecked') else ' '}] {step.get('displayName')}")
    return "\n".join(lines)


def list_tasks(
    settings: Settings,
    account: str | None = None,
    list_id: str | None = None,
    include_completed: bool = False,
) -> str:
    label = resolve_account(settings, account)
    lid = list_id or _default_list_id(settings, label)
    params = {"$expand": "checklistItems"}
    if not include_completed:
        params["$filter"] = "status ne 'completed'"
    items = _request(settings, label, "GET", f"/me/todo/lists/{lid}/tasks", params=params).get("value", [])
    if not items:
        return "No tasks."
    return "\n".join(format_task(t) for t in items)


def create_task(
    settings: Settings,
    title: str,
    account: str | None = None,
    list_id: str | None = None,
    due: str | None = None,
    notes: str | None = None,
) -> str:
    label = resolve_account(settings, account)
    lid = list_id or _default_list_id(settings, label)
    body: dict[str, Any] = {"title": title}
    if due:
        body["dueDateTime"] = due_body(due)
    if notes:
        body["body"] = {"content": notes, "contentType": "text"}
    created = _request(settings, label, "POST", f"/me/todo/lists/{lid}/tasks", json=body)
    return f"Created: {format_task(created)}"


def complete_task(
    settings: Settings, task_id: str, account: str | None = None, list_id: str | None = None
) -> str:
    label = resolve_account(settings, account)
    lid = list_id or _default_list_id(settings, label)
    updated = _request(
        settings, label, "PATCH", f"/me/todo/lists/{lid}/tasks/{task_id}", json={"status": "completed"}
    )
    return f"Completed: {format_task(updated)}"
