"""Lessons: a correction, once given, shapes every later reply and digest."""

from pathlib import Path

from quartermaster import agent, lessons
from quartermaster.config import Settings


def test_add_dates_and_dedupes(tmp_path: Path):
    path = tmp_path / "facts" / "lessons.md"
    assert lessons.add(path, "Finance events go on the   Finance calendar.")
    assert not lessons.add(path, "finance events go on the finance calendar.")
    text = path.read_text("utf-8")
    assert text.startswith("# Lessons")
    assert text.count("- 20") == 1 and "Finance events go on the Finance calendar." in text


def test_for_prompt_is_empty_without_lessons(tmp_path: Path):
    assert lessons.for_prompt(None) == ""
    assert lessons.for_prompt(tmp_path / "missing.md") == ""
    path = tmp_path / "lessons.md"
    path.write_text(lessons.HEADER, "utf-8")
    assert lessons.for_prompt(path) == ""


def test_for_prompt_keeps_the_newest_when_long(tmp_path: Path):
    path = tmp_path / "lessons.md"
    for i in range(400):
        lessons.add(path, f"lesson number {i} " + "x" * 20)
    block = lessons.for_prompt(path)
    assert "lesson number 399" in block and "lesson number 0 " not in block
    assert len(block) < lessons.MAX_CHARS + 200


def test_owner_and_digest_read_lessons_fresh_every_turn(tmp_path: Path):
    settings = Settings(vault=tmp_path)
    owner = agent.owner_profile(settings)
    assert "record_lesson" in agent.OWNER_LIMITS
    assert "Lessons" not in agent._options(owner).system_prompt["append"]
    lessons.add(lessons.lessons_path(settings), "Never book anything before 9am.")
    # Same profile object, as the bot holds one for its whole run.
    assert "Never book anything before 9am." in agent._options(owner).system_prompt["append"]
    assert "Never book anything before 9am." in agent._options(agent.digest_profile(settings)).system_prompt["append"]
    assert agent.public_profile(settings).lessons_file is None


def test_record_lesson_tool(tmp_path: Path, monkeypatch):
    from quartermaster import servers
    from quartermaster.servers import qm

    monkeypatch.setattr(servers, "settings", lambda: Settings(vault=tmp_path))
    monkeypatch.setattr(qm, "settings", lambda: Settings(vault=tmp_path))
    assert "Recorded" in qm.record_lesson("Keep digest lines under 80 characters.")
    assert qm.record_lesson("keep digest lines under 80 characters.") == "Already recorded."
    assert "80 characters" in (tmp_path / "facts" / "lessons.md").read_text("utf-8")
    owner = agent.owner_profile(Settings(vault=tmp_path))
    assert agent.check_tool(owner, "mcp__qm__record_lesson", {}) is None
