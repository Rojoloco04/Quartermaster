"""Spotify, for taste-matching only.

One account, one cached token file, in the same per-user tokens directory as
Google's and Microsoft's. Read-only scopes by design - this app never
manages playlists or controls playback, it only reads what the owner
listens to so the (not-yet-built) events digest can tell a show worth
travelling for from one that isn't.

Spotify's redirect URI has to match the app dashboard exactly (no wildcard
port, unlike Google's `http://localhost`), so it's a fixed constant here -
register REDIRECT_URI verbatim in the Spotify Developer Dashboard.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import Settings

# Exact match required in the Spotify Developer Dashboard - Settings ->
# Redirect URIs. Not a real endpoint; spotipy runs a local server on this
# port for the one redirect and then stops listening.
REDIRECT_URI = "http://127.0.0.1:8765/callback"

# Read-only: top artists/tracks and the saved-tracks library are enough for
# a taste signal. No playlist or playback scopes - there's nothing here that
# writes to the account.
SCOPES = "user-top-read user-library-read"

_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_TIME_RANGES = {"short_term", "medium_term", "long_term"}


class SpotifyError(RuntimeError):
    pass


# --- Accounts and tokens ------------------------------------------------------


def token_path(settings: Settings, label: str) -> Path:
    if not _LABEL.match(label):
        raise SpotifyError(
            f"Account label {label!r} must be lowercase letters, digits, - or _ (max 32)."
        )
    return settings.tokens_dir / f"spotify-{label}.json"


def accounts(settings: Settings) -> list[str]:
    """Labels of every authorised account, in a stable order."""
    if not settings.tokens_dir.exists():
        return []
    return sorted(p.stem.removeprefix("spotify-") for p in settings.tokens_dir.glob("spotify-*.json"))


def _auth_manager(settings: Settings, label: str, *, open_browser: bool):
    from spotipy.cache_handler import CacheFileHandler
    from spotipy.oauth2 import SpotifyOAuth

    settings.require("spotify_client_id", "spotify_client_secret")
    path = token_path(settings, label)
    path.parent.mkdir(parents=True, exist_ok=True)
    return SpotifyOAuth(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        cache_handler=CacheFileHandler(cache_path=str(path)),
        open_browser=open_browser,
    )


def authorize(settings: Settings, label: str) -> str:
    """Run the browser consent flow for one account and cache its token.

    Returns the signed-in account's display name (or id, if none is set) so
    the caller can confirm the right account was picked in the browser.
    """
    from spotipy import Spotify
    from spotipy.exceptions import SpotifyOauthError

    auth_manager = _auth_manager(settings, label, open_browser=True)
    try:
        auth_manager.get_access_token(as_dict=False)
    except SpotifyOauthError as exc:
        raise SpotifyError(f"No Spotify permission was granted, so nothing was saved. ({exc})") from exc

    me = Spotify(auth_manager=auth_manager).current_user()
    return me.get("display_name") or me.get("id", "?")


def _client(settings: Settings, label: str):
    from spotipy import Spotify

    path = token_path(settings, label)
    if not path.exists():
        known = ", ".join(accounts(settings)) or "none"
        raise SpotifyError(
            f"No Spotify account called {label!r} (authorised: {known}). "
            f"Run: qm auth spotify {label}"
        )
    # open_browser=False: a read call must never pop a browser window on its
    # own. If the cached token is unusable, this raises instead - the error
    # path below turns that into "re-authorise", not a surprise popup.
    auth_manager = _auth_manager(settings, label, open_browser=False)
    return Spotify(auth_manager=auth_manager)


def resolve_account(settings: Settings, account: str | None) -> str:
    """One named account, or the only one, when there's no ambiguity."""
    known = accounts(settings)
    if not known:
        raise SpotifyError("No Spotify accounts are authorised yet. Run: qm auth spotify <label>")
    if account:
        if account not in known:
            raise SpotifyError(f"No Spotify account called {account!r}. Authorised: {', '.join(known)}.")
        return account
    if len(known) > 1:
        raise SpotifyError(f"Say which account: {', '.join(known)}.")
    return known[0]


def _time_range(value: str) -> str:
    if value not in _TIME_RANGES:
        raise SpotifyError(f"time_range must be one of {', '.join(sorted(_TIME_RANGES))}.")
    return value


# --- Taste signal ---------------------------------------------------------------


def top_artists(
    settings: Settings, account: str | None = None, time_range: str = "medium_term", limit: int = 10
) -> str:
    label = resolve_account(settings, account)
    sp = _client(settings, label)
    items = sp.current_user_top_artists(
        limit=max(1, min(int(limit), 50)), time_range=_time_range(time_range)
    ).get("items", [])
    if not items:
        return "No top artists for that time range."
    return "\n".join(f"- {a['name']}  ({', '.join(a.get('genres', [])[:3]) or 'no genre tags'})" for a in items)


def top_tracks(
    settings: Settings, account: str | None = None, time_range: str = "medium_term", limit: int = 10
) -> str:
    label = resolve_account(settings, account)
    sp = _client(settings, label)
    items = sp.current_user_top_tracks(
        limit=max(1, min(int(limit), 50)), time_range=_time_range(time_range)
    ).get("items", [])
    if not items:
        return "No top tracks for that time range."
    return "\n".join(f"- {t['name']} — {', '.join(a['name'] for a in t['artists'])}" for t in items)


def saved_tracks(settings: Settings, account: str | None = None, limit: int = 10) -> str:
    label = resolve_account(settings, account)
    sp = _client(settings, label)
    items = sp.current_user_saved_tracks(limit=max(1, min(int(limit), 50))).get("items", [])
    if not items:
        return "No saved tracks."
    return "\n".join(
        f"- {it['track']['name']} — {', '.join(a['name'] for a in it['track']['artists'])}" for it in items
    )
