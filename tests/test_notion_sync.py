"""Path building and frontmatter for the Notion mirror.

These are pure functions, and they're where a silent bug would be worst: a
wrong path means a page is mirrored twice under two names, and a dropped
truncation flag means the agent answers confidently from half a page.
"""

from pathlib import Path

from quartermaster.notion_sync import _frontmatter, _vault_path, slugify


def page(pid: str, title: str, parent: str | None = None) -> dict:
    obj: dict = {
        "id": pid,
        "object": "page",
        "url": f"https://notion.so/{pid}",
        "last_edited_time": "2026-09-20T10:00:00.000Z",
        "properties": {
            "title": {"type": "title", "title": [{"plain_text": title}]},
        },
    }
    obj["parent"] = {"type": "page_id", "page_id": parent} if parent else {"type": "workspace"}
    return obj


class TestSlugify:
    def test_basic(self):
        assert slugify("Car Washing Instructions") == "car-washing-instructions"

    def test_strips_punctuation_and_accents(self):
        assert slugify("Ana's Café — Notes!") == "ana-s-cafe-notes"

    def test_empty_input_still_yields_a_name(self):
        assert slugify("") == "untitled"
        assert slugify("!!!") == "untitled"

    def test_truncates_long_titles(self):
        assert len(slugify("x" * 200)) <= 60

    def test_no_trailing_separator_after_truncation(self):
        # A trailing dash would produce paths like "some-title-.md".
        assert not slugify("a" * 59 + " bbbb").endswith("-")


class TestVaultPath:
    def test_nests_under_parents(self, tmp_path: Path):
        index = {
            "aaa": page("aaa", "School"),
            "bbb": page("bbb", "Coursework", parent="aaa"),
            "ccc": page("ccc", "Notes", parent="bbb"),
        }
        result = _vault_path("ccc", index, tmp_path)
        assert result.relative_to(tmp_path).as_posix() == "school/coursework/notes-ccc.md"

    def test_id_suffix_disambiguates_siblings(self, tmp_path: Path):
        index = {
            "p1": page("p1", "Notes"),
            "p2": page("p2", "Notes"),
        }
        a = _vault_path("p1", index, tmp_path)
        b = _vault_path("p2", index, tmp_path)
        assert a != b, "two pages with the same title must not collide"

    def test_cycle_does_not_hang(self, tmp_path: Path):
        # Notion shouldn't produce these, but a malformed parent chain must not
        # spin forever during an unattended nightly sync.
        index = {
            "x": page("x", "X", parent="y"),
            "y": page("y", "Y", parent="x"),
        }
        assert _vault_path("x", index, tmp_path).suffix == ".md"

    def test_unknown_parent_falls_back(self, tmp_path: Path):
        index = {"solo": page("solo", "Solo", parent="missing")}
        result = _vault_path("solo", index, tmp_path)
        assert result.relative_to(tmp_path).as_posix() == "solo-solo.md"


class TestFrontmatter:
    def test_records_identity(self):
        fm = _frontmatter(page("abc", "Car Washing"), "car-washing-abc.md", False, [])
        assert 'notion_id: "abc"' in fm
        assert 'title: "Car Washing"' in fm
        assert "mirror: true" in fm
        assert "truncated" not in fm

    def test_truncation_is_visible_not_silent(self):
        fm = _frontmatter(page("abc", "Huge"), "huge-abc.md", True, ["b1", "b2"])
        assert "truncated: true" in fm
        assert "unknown_blocks: 2" in fm
        # The agent reads prose, not just frontmatter, so warn in the body too.
        assert "incomplete" in fm.lower()

    def test_quotes_in_title_do_not_break_yaml(self):
        fm = _frontmatter(page("abc", 'The "Big" Page'), "p-abc.md", False, [])
        title_line = next(ln for ln in fm.splitlines() if ln.startswith("title:"))
        assert title_line.count('"') == 2, f"unbalanced quotes would break YAML: {title_line}"
