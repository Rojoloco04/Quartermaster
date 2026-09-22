"""The dev queue: filled in plain conversation, worked by hand in Claude Code."""

from pathlib import Path

from quartermaster import agent, dev_queue
from quartermaster.config import Settings


def test_add_tags_the_source_and_list_shows_open_items(tmp_path: Path):
    path = tmp_path / "dev-queue.md"
    assert "empty" in dev_queue.listing(path)
    dev_queue.add(path, "make the   digest\nshorter")
    dev_queue.add(path, "retry Spotify once on a 5xx", source="noticed")
    path.write_text(path.read_text().replace("- [ ]", "- [x]", 1))  # owner ticks the first off
    assert [text for _, text in dev_queue.open_items(path)] == ["retry Spotify once on a 5xx (noticed)"]


def test_the_owner_agent_may_append_to_the_queue(tmp_path: Path):
    # Nothing works the queue unattended, so a person reviews every item: the
    # agent writing here is how "make the digest shorter" gets queued at all.
    owner = agent.owner_profile(Settings(vault=tmp_path))
    assert agent.check_tool(owner, "Edit", {"file_path": "90-System/dev-queue.md"}) is None
    assert "dev-queue.md" in agent.OWNER_LIMITS and "(noticed)" in agent.OWNER_LIMITS
