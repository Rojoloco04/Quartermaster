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
    },
    "notion": {
        # A page must be untouched this long AND look unfinished to be called stale.
        "stale_after_days": 90,
    },
    "digest": {
        "weekday": "sunday",
        "hour": 18,
        "calendar_days_ahead": 7,
    },
    "quiet_hours": {
        "start": 22,
        "end": 8,
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
    claude_cli: str | None
    notion_token: str | None
    notion_claude_page_id: str | None
    discord_bot_token: str | None
    discord_owner_id: int | None
    prefs: dict = field(default_factory=dict)

    # --- Vault paths. Everything else asks here rather than joining strings. ---

    @property
    def facts_dir(self) -> Path:
        return self.vault / "facts"

    @property
    def inbox_dir(self) -> Path:
        return self.vault / "inbox"

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
    def pending_file(self) -> Path:
        return self.system_dir / "pending.md"

    @property
    def config_file(self) -> Path:
        return self.system_dir / "config.toml"

    @property
    def db_path(self) -> Path:
        return self.system_dir / "state.db"

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
        notion_claude_page_id=os.getenv("NOTION_CLAUDE_PAGE_ID") or None,
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN") or None,
        discord_owner_id=int(owner_raw) if owner_raw.isdigit() else None,
        prefs=prefs,
    )
