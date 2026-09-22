"""A whole sync run against a fake Notion: what's deleted in Notion leaves the
mirror, even files state.db never knew about, and a mass deletion is held back."""

from pathlib import Path

import pytest

from quartermaster import notion_sync
from quartermaster.config import DEFAULTS, Settings
from quartermaster.integrations.notion import PageMarkdown


def page(pid: str, title: str, parent: str | None = None, edited: str = "2026-09-20T10:00:00.000Z") -> dict:
    return {
        "id": pid, "object": "page", "url": f"https://notion.so/{pid}", "last_edited_time": edited,
        "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
        "parent": {"type": "page_id", "page_id": parent} if parent else {"type": "workspace"},
    }


class FakeNotion:
    pages: list[dict] = []
    fetched: list[str] = []

    def __init__(self, token: str) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def search_pages(self):
        return iter(FakeNotion.pages)

    def page_markdown(self, page_id: str) -> PageMarkdown:
        FakeNotion.fetched.append(page_id)
        return PageMarkdown(markdown=f"Body of {page_id}", truncated=False, unknown_block_ids=[])


ROOT, GAMING, COOKING, OSU = "a" * 32, "b" * 31 + "1", "c" * 31 + "2", "d" * 31 + "3"


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setattr(notion_sync, "NotionClient", FakeNotion)
    FakeNotion.pages = [page(ROOT, "Hobbies"), page(GAMING, "Gaming", ROOT),
                        page(COOKING, "Cooking", ROOT), page(OSU, "osu", GAMING)]
    FakeNotion.fetched = []
    s = Settings(vault=tmp_path / "Vault", notion_token="t", prefs=DEFAULTS)
    (s.vault / "System").mkdir(parents=True)
    return s


def mirror(s: Settings) -> set[str]:
    return {p.relative_to(s.notion_dir).as_posix() for p in s.notion_dir.rglob("*.md")}


def test_a_page_deleted_in_notion_leaves_the_mirror(settings):
    notion_sync.sync(settings)
    assert f"hobbies/cooking-{COOKING[-8:]}.md" in mirror(settings)
    FakeNotion.pages = [p for p in FakeNotion.pages if p["id"] != COOKING]
    stats = notion_sync.sync(settings)
    assert stats.removed == 1 and not any("cooking" in m for m in mirror(settings))


def test_orphans_state_db_never_knew_about_are_removed(settings):
    notion_sync.sync(settings)
    stray = settings.notion_dir / "hobbies" / "old-layout-12345678.md"  # e.g. before a state.db rebuild
    stray.write_text("stale", "utf-8")
    (settings.notion_dir / "README.md").write_text("# Notion mirror", "utf-8")
    stats = notion_sync.sync(settings)
    assert stats.removed == 1 and not stray.exists()
    assert (settings.notion_dir / "README.md").exists()  # the mirror's own README stays


def test_a_mass_deletion_is_held_back_unless_forced(settings):
    notion_sync.sync(settings)
    before = mirror(settings)
    FakeNotion.pages = FakeNotion.pages[:1]  # integration unshared from the subpages, say
    stats = notion_sync.sync(settings)
    assert stats.held_back == 3 and stats.removed == 0 and mirror(settings) == before
    assert "HELD BACK" in stats.summary()
    FakeNotion.pages = []
    assert notion_sync.sync(settings).held_back == 4  # Notion returned nothing at all
    FakeNotion.pages = [page(ROOT, "Hobbies")]
    assert notion_sync.sync(settings, force=True).removed == 3


def test_renaming_a_parent_moves_its_children(settings):
    notion_sync.sync(settings)
    FakeNotion.fetched = []
    # Only the parent's own edit time changes; the child's doesn't.
    FakeNotion.pages[1] = page(GAMING, "Video Games", ROOT, edited="2026-09-22T10:00:00.000Z")
    notion_sync.sync(settings)
    files = mirror(settings)
    assert f"hobbies/video-games/osu-{OSU[-8:]}.md" in files
    assert not any(f.startswith("hobbies/gaming") for f in files)
    assert OSU in FakeNotion.fetched  # re-fetched to its new path, links rebuilt


def test_sync_is_callable_from_chat(settings, monkeypatch):
    from quartermaster import servers
    from quartermaster.servers import qm

    monkeypatch.setattr(servers, "settings", lambda: settings)
    assert "Notion sync done: 4 pages scanned, 4 written" in qm.sync_notion()
