"""Path building and frontmatter for the Notion mirror.

These are pure functions, and they're where a silent bug would be worst: a
wrong path means a page is mirrored twice under two names, and a dropped
truncation flag means the agent answers confidently from half a page.
"""

from pathlib import Path

from quartermaster.notion_sync import _frontmatter, _vault_path, assign_paths, slugify


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


class TestNonAsciiTitles:
    """Regression: three CJK-titled pages once collapsed onto one file.

    ASCII-folding with errors='ignore' deletes CJK entirely, so 中文, 日本語 and
    한국어 all slugified to 'untitled'. Combined with a non-unique id suffix,
    two of the three pages were silently overwritten and lost.
    """

    def test_cjk_titles_survive_slugification(self):
        for title in ("中文", "日本語", "한국어"):
            assert slugify(title) == title.lower(), f"{title} must not be erased"

    def test_distinct_cjk_titles_produce_distinct_slugs(self):
        slugs = {slugify(t) for t in ("中文", "日本語", "한국어")}
        assert len(slugs) == 3

    def test_mixed_script_still_prefers_ascii(self):
        assert slugify("Notes 中文") == "notes"

    def test_illegal_filename_characters_are_removed(self):
        assert "/" not in slugify("中文/日本語")
        assert ":" not in slugify("中文:test")

    def test_windows_reserved_names_are_escaped(self):
        # 'con.md' is unopenable on Windows.
        assert slugify("CON") != "con"
        assert slugify("nul") != "nul"


class TestPathUniqueness:
    """No two pages may ever claim the same file. Losing one is silent."""

    def test_identical_titles_and_id_prefix_do_not_collide(self):
        # Real ids from the workspace: they share the first eight characters.
        index = {
            "28b7c599fefe80658df2f6c3aebbb64c": page("28b7c599fefe80658df2f6c3aebbb64c", "中文"),
            "28b7c599fefe800eaaabc1dc6021200a": page("28b7c599fefe800eaaabc1dc6021200a", "日本語"),
            "28b7c599fefe80f2924bd007d6ee240e": page("28b7c599fefe80f2924bd007d6ee240e", "한국어"),
        }
        paths = assign_paths(index, Path("/vault/notion"))
        assert len(set(paths.values())) == 3

    def test_pages_with_the_same_title_do_not_collide(self):
        index = {
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": page("a" * 32, "Notes"),
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": page("b" * 32, "Notes"),
        }
        paths = assign_paths(index, Path("/vault/notion"))
        assert len(set(paths.values())) == 2

    def test_case_only_differences_do_not_collide(self):
        # NTFS is case-insensitive: 'Notes-x.md' and 'notes-x.md' are one file.
        index = {
            "1" * 32: page("1" * 32, "NOTES"),
            "2" * 32: page("2" * 32, "notes"),
        }
        paths = assign_paths(index, Path("/vault/notion"))
        assert len({str(p).lower() for p in paths.values()}) == 2

    def test_every_page_gets_a_path(self):
        index = {f"{i:032x}": page(f"{i:032x}", "Same Title") for i in range(25)}
        paths = assign_paths(index, Path("/vault/notion"))
        assert len(paths) == 25
        assert len(set(paths.values())) == 25

    def test_assignment_is_deterministic(self):
        index = {
            "28b7c599fefe80658df2f6c3aebbb64c": page("28b7c599fefe80658df2f6c3aebbb64c", "中文"),
            "28b7c599fefe800eaaabc1dc6021200a": page("28b7c599fefe800eaaabc1dc6021200a", "日本語"),
        }
        first = assign_paths(index, Path("/vault/notion"))
        second = assign_paths(index, Path("/vault/notion"))
        assert first == second, "an unstable mapping would churn files on every sync"
