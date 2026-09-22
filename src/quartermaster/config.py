"""Configuration.

Two sources, deliberately separated:

- ``.env`` in the repo holds secrets. Gitignored, never committed.
- ``90-System/config.toml`` in the vault holds preferences. Committed to the
  vault's own repo, hand-editable, and the thing you actually tune.

Anything you would be upset to leak goes in the first. Anything you would want
to read and change goes in the second.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

# Defaults for every preference. The vault's config.toml overrides these, so a
# missing or partial file is always safe.
DEFAULTS: dict = {
    "home": {
        "label": "St. Louis, MO",
        # Geohash for downtown STL. Ticketmaster's geoPoint takes a geohash,
        # not a raw lat/long pair.
        "geopoint": "9yzg",
        "latitude": 38.6270,
        "longitude": -90.1994,
    },
    # Distance bands for the events section. Each is queried separately: it
    # sets the bar for inclusion, and keeps each query under Ticketmaster's
    # 1000-result deep-paging cap.
    "events": {
        "bands": [
            {"name": "local", "min_miles": 0, "max_miles": 60, "bar": "low"},
            {"name": "day_trip", "min_miles": 60, "max_miles": 250, "bar": "medium"},
            {"name": "weekend", "min_miles": 250, "max_miles": 500, "bar": "high"},
        ],
        # How far ahead to look for events worth travelling to. Longer than
        # the calendar window on purpose - a show worth planning a trip
        # around is usually booked weeks out, not found seven days ahead.
        "window_days": 60,
    },
    "notion": {
        # A page must be untouched this long AND look unfinished to be called stale.
        "stale_after_days": 90,
        # The one Notion page (and its sub-pages) the agent may write to.
        "claude_page_id": "",
    },
    "wishlist": {
        # The Notion page id (from its URL) whose to-do/bulleted/bookmark
        # items get price-checked. A plain hand-edited page, not a database -
        # an item is checked once it has a link, whether that's a bookmark
        # block or a hyperlink on a checklist line. Blank skips price checks.
        "page_id": "",
    },
    "chat": {
        # Minutes without activity (a message either way, from any surface)
        # after which the next message starts a fresh conversation. Continuing
        # re-sends the whole conversation every turn, so a stale one is paid
        # for again and again. 0 never starts fresh on its own.
        "fresh_after_minutes": 5,
    },
    "digest": {
        "weekday": "sunday",
        "hour": 18,
        "calendar_days_ahead": 7,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base, recursing into nested dicts.

    Lists are replaced wholesale, not concatenated — if you redefine the event
    bands you mean to redefine all of them.
    """
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class Settings:
    vault: Path
    claude_cli: str | None = None
    notion_token: str | None = None
    discord_bot_token: str | None = None
    discord_owner_id: int | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_tenant_id: str = "consumers"
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    ticketmaster_api_key: str | None = None
    klipy_api_key: str | None = None
    lastfm_api_key: str | None = None
    lastfm_user: str | None = None
    prefs: dict = field(default_factory=dict)

    # --- Vault paths. Everything else asks here rather than joining strings. ---

    @property
    def facts_dir(self) -> Path:
        return self.vault / "facts"

    @property
    def notion_dir(self) -> Path:
        return self.vault / "notion"

    @property
    def digests_dir(self) -> Path:
        return self.vault / "digests"

    @property
    def system_dir(self) -> Path:
        return self.vault / "90-System"

    @property
    def muted_file(self) -> Path:
        return self.system_dir / "muted.md"

    @property
    def db_path(self) -> Path:
        return self.system_dir / "state.db"

    @property
    def tokens_dir(self) -> Path:
        """OAuth tokens. In the per-user config directory, outside both repos:
        a refresh token grants a whole inbox and belongs in neither."""
        from platformdirs import user_config_path

        return user_config_path("quartermaster", appauthor=False) / "tokens"

    @property
    def log_path(self) -> Path:
        """Where every tool call, moderation action and turn outcome is
        written - durable, not just whatever console happens to be attached
        when the bot is backgrounded. Per-user, outside both repos, same
        family as tokens_dir."""
        from platformdirs import user_log_path

        return user_log_path("quartermaster", appauthor=False) / "quartermaster.log"

    def workspace(self, name: str) -> Path:
        """A working directory for a profile that must not run in the vault.

        Per-user, outside both repos, and never derived from ``self.vault``.
        Two reasons: containment (the public and parser profiles must not load
        the vault), and session hygiene - every run leaves a session file for
        its cwd, and ``--continue`` resumes the newest one there, so a digest
        run in the vault would hijack the owner's shared conversation.
        """
        from platformdirs import user_data_path

        return user_data_path("quartermaster", appauthor=False) / "workspaces" / name

    @property
    def public_workspace(self) -> Path:
        return self.workspace("public")

    def require(self, *names: str) -> None:
        """Fail early and by name when a secret a task needs is absent.

        Raising here beats a 401 from a third party three calls later.
        """
        missing = [n for n in names if not getattr(self, n, None)]
        if missing:
            raise RuntimeError(
                "Missing required setting(s): "
                + ", ".join(missing)
                + f". Add them to {REPO_ROOT / '.env'} (see .env.example)."
            )


def current_prefs(settings: Settings) -> dict:
    """Preferences as config.toml says right now, for what a long-running
    process should pick up without a restart. Falls back to what was loaded at
    startup if the file is gone or doesn't parse."""
    config_file = settings.system_dir / "config.toml"
    try:
        with config_file.open("rb") as fh:
            return _deep_merge(DEFAULTS, tomllib.load(fh))
    except (OSError, tomllib.TOMLDecodeError):
        return settings.prefs


def load_settings(vault_override: Path | None = None) -> Settings:
    load_dotenv(REPO_ROOT / ".env")

    vault_raw = vault_override or os.getenv("QM_VAULT_PATH")
    if not vault_raw:
        raise RuntimeError(
            "QM_VAULT_PATH is not set. Copy .env.example to .env and point it "
            "at where you want the vault to live."
        )
    vault = Path(vault_raw).expanduser()

    prefs = DEFAULTS
    config_file = vault / "90-System" / "config.toml"
    if config_file.exists():
        with config_file.open("rb") as fh:
            prefs = _deep_merge(DEFAULTS, tomllib.load(fh))

    owner_raw = os.getenv("DISCORD_OWNER_ID") or ""

    return Settings(
        vault=vault,
        claude_cli=os.getenv("QM_CLAUDE_CLI") or None,
        notion_token=os.getenv("NOTION_TOKEN") or None,
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN") or None,
        discord_owner_id=int(owner_raw) if owner_raw.isdigit() else None,
        google_client_id=os.getenv("GOOGLE_CLIENT_ID") or None,
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET") or None,
        microsoft_client_id=os.getenv("MS_CLIENT_ID") or None,
        microsoft_tenant_id=os.getenv("MS_TENANT_ID") or "consumers",
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID") or None,
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET") or None,
        ticketmaster_api_key=os.getenv("TICKETMASTER_API_KEY") or None,
        klipy_api_key=os.getenv("KLIPY_API_KEY") or None,
        lastfm_api_key=os.getenv("LASTFM_API_KEY") or None,
        lastfm_user=os.getenv("LASTFM_USER") or None,
        prefs=prefs,
    )
