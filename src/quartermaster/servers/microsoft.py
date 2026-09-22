"""Microsoft To Do as MCP tools.

Thin on purpose: validation, formatting and the Graph calls live in
``integrations.microsoft``. This file only maps tool calls onto it and turns
failures into messages the model can act on.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..config import Settings, load_settings
from ..integrations import microsoft

server = MCPServer("microsoft")
_settings: Settings | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def _run(fn: Callable[..., str], *args, **kwargs) -> str:
    try:
        return fn(_get_settings(), *args, **kwargs)
    except microsoft.MicrosoftError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - httpx and friends
        raise ToolError(f"Microsoft To Do request failed: {exc}") from exc


@server.tool()
def list_accounts() -> str:
    """List the labels of the authorised Microsoft accounts (e.g. personal)."""
    labels = microsoft.accounts(_get_settings())
    return ", ".join(labels) if labels else "None yet. The owner runs: qm auth microsoft <label>"


@server.tool()
def list_task_lists(account: str | None = None) -> str:
    """List To Do task lists for one account, or the only account if none is named."""
    return _run(microsoft.list_task_lists, account)


@server.tool()
def list_tasks(account: str | None = None, list_id: str | None = None, include_completed: bool = False) -> str:
    """List tasks in one list. Defaults to the account's default list and hides
    completed tasks unless include_completed is set."""
    return _run(microsoft.list_tasks, account, list_id, include_completed)


@server.tool()
def create_task(
    title: str,
    account: str | None = None,
    list_id: str | None = None,
    due: str | None = None,
    notes: str | None = None,
) -> str:
    """Add a task to the default list, or list_id if given. due is a bare date
    (YYYY-MM-DD) or ISO 8601 datetime."""
    return _run(microsoft.create_task, title, account, list_id, due, notes)


@server.tool()
def complete_task(task_id: str, account: str | None = None, list_id: str | None = None) -> str:
    """Mark a task complete by its id (from list_tasks)."""
    return _run(microsoft.complete_task, task_id, account, list_id)


def main() -> None:
    server.run("stdio")
