"""The stale-page heuristic: old and still looking unfinished. Pure string
tests - `looks_unfinished` never touches disk."""

from quartermaster.stale import item_id, looks_unfinished


class TestLooksUnfinished:
    def test_a_healthy_reference_page_is_not_flagged(self):
        body = (
            "---\ntitle: \"Car maintenance schedule\"\n---\n\n"
            "## Oil changes\n\nEvery 5,000 miles, or every six months, whichever comes first.\n\n"
            "## Tire rotation\n\nEvery other oil change.\n"
        )
        unfinished, reason = looks_unfinished(body)
        assert unfinished is False
        assert reason == ""

    def test_open_checkboxes_are_flagged(self):
        body = "---\ntitle: \"x\"\n---\n\nSome context here that is long enough.\n\n- [ ] buy paint\n- [x] buy brush\n"
        unfinished, reason = looks_unfinished(body)
        assert unfinished is True
        assert "checkbox" in reason

    def test_an_empty_section_is_flagged(self):
        body = (
            "---\ntitle: \"x\"\n---\n\n"
            "## Intro\n\nThis section has real content in it, plenty of it actually.\n\n"
            "## Notes\n\n## Next section\n\nMore content here.\n"
        )
        unfinished, reason = looks_unfinished(body)
        assert unfinished is True
        assert "Notes" in reason

    def test_a_trailing_empty_section_at_eof_is_flagged(self):
        body = "---\ntitle: \"x\"\n---\n\n## Real section\n\nPlenty of content in here to pass the length bar.\n\n## Todo\n"
        unfinished, reason = looks_unfinished(body)
        assert unfinished is True
        assert "Todo" in reason

    def test_a_bare_todo_marker_is_flagged(self):
        body = "---\ntitle: \"x\"\n---\n\nThis page has enough characters to pass the stub check easily.\n\nTODO\n"
        unfinished, reason = looks_unfinished(body)
        assert unfinished is True
        assert "TODO" in reason

    def test_a_tiny_body_is_a_stub(self):
        body = "---\ntitle: \"x\"\n---\n\nnot much here"
        unfinished, reason = looks_unfinished(body)
        assert unfinished is True
        assert "empty" in reason

    def test_frontmatter_alone_does_not_count_toward_content_length(self):
        body = '---\ntitle: "A page with a long title that says nothing else"\nmirror: true\n---\n'
        unfinished, _ = looks_unfinished(body)
        assert unfinished is True


class TestItemId:
    def test_uses_the_full_page_id(self):
        assert item_id("28b7c599abcdef0123456789") == "stale:28b7c599abcdef0123456789"
