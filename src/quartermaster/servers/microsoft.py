"""Microsoft To Do as MCP tools."""

from __future__ import annotations

from mcp.server import MCPServer

from . import list_accounts as _accounts, run, settings
from ..integrations import microsoft

server = MCPServer("microsoft")


@server.tool()
def list_accounts() -> str:
    """List the labels of the authorised Microsoft accounts (e.g. personal)."""
    return _accounts("microsoft", microsoft.accounts(settings()))


@server.tool()
def list_task_lists(account: str | None = None) -> str:
    """List To Do task lists for one account, or the only account if none is named."""
    return run("microsoft", microsoft.list_task_lists, account)


@server.tool()
def list_tasks(account: str | None = None, list_id: str | None = None, include_completed: bool = False) -> str:
    """List tasks in one list. Defaults to the account's default list and hides
    completed tasks unless include_completed is set."""
    return run("microsoft", microsoft.list_tasks, account, list_id, include_completed)


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
    return run("microsoft", microsoft.create_task, title, account, list_id, due, notes)


@server.tool()
def complete_task(task_id: str, account: str | None = None, list_id: str | None = None) -> str:
    """Mark a task complete by its id (from list_tasks)."""
    return run("microsoft", microsoft.complete_task, task_id, account, list_id)

