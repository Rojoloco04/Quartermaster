"""Account plumbing shared by the OAuth integrations (google, microsoft, spotify).

One token file per account, ``<service>-<label>.json`` in ``Settings.tokens_dir``:
the per-user config directory, outside both repos, because a refresh token is
a long-lived credential for a whole inbox or calendar.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import Settings

_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def token_path(settings: Settings, service: str, label: str, error: type[Exception]) -> Path:
    if not _LABEL.match(label):
        raise error(f"Account label {label!r} must be lowercase letters, digits, - or _ (max 32).")
    return settings.tokens_dir / f"{service}-{label}.json"


def accounts(settings: Settings, service: str) -> list[str]:
    """Labels of every authorised account, in a stable order."""
    if not settings.tokens_dir.exists():
        return []
    return sorted(p.stem.removeprefix(f"{service}-") for p in settings.tokens_dir.glob(f"{service}-*.json"))


def existing_token(settings: Settings, service: str, label: str, error: type[Exception]) -> Path:
    """The token file for ``label``, or an error naming what is authorised."""
    path = token_path(settings, service, label, error)
    if not path.exists():
        known = ", ".join(accounts(settings, service)) or "none"
        raise error(
            f"No {service.capitalize()} account called {label!r} (authorised: {known}). "
            f"Run: qm auth {service} {label}"
        )
    return path


def resolve_one(settings: Settings, service: str, account: str | None, error: type[Exception]) -> str:
    """One named account, or the only one, when there's no ambiguity."""
    known = accounts(settings, service)
    if not known:
        raise error(f"No {service.capitalize()} accounts are authorised yet. Run: qm auth {service} <label>")
    if account:
        if account not in known:
            raise error(f"No {service.capitalize()} account called {account!r}. Authorised: {', '.join(known)}.")
        return account
    if len(known) > 1:
        raise error(f"Say which account: {', '.join(known)}.")
    return known[0]
