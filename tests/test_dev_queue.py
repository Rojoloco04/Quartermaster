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


def test_agents_queue_only_through_the_tool(tmp_path: Path):
    # A remembered file convention was ignored in real use; a tool is not. The
    # file itself is protected so every entry is one tagged, reviewable line.
    owner = agent.owner_profile(Settings(vault=tmp_path))
    assert agent.check_tool(owner, "mcp__qm__queue_change", {}) is None
    assert agent.check_tool(owner, "Edit", {"file_path": "90-System/dev-queue.md"})
    assert "queue_change" in agent.OWNER_LIMITS


def test_queue_change_tool_tags_and_dedupes(tmp_path: Path, monkeypatch):
    from quartermaster import servers
    from quartermaster.servers import qm

    monkeypatch.setattr(servers, "settings", lambda: Settings(vault=tmp_path))
    monkeypatch.setattr(qm, "settings", lambda: Settings(vault=tmp_path))
    assert "Queued (1 open)" in qm.queue_change("Add weather to the digest")
    assert qm.queue_change("add weather to the digest") == "Already in the dev queue."
    qm.queue_change("Retry Spotify on a 5xx", source="noticed")
    texts = [t for _, t in dev_queue.open_items(dev_queue.queue_path(Settings(vault=tmp_path)))]
    assert texts == ["Add weather to the digest (you)", "Retry Spotify on a 5xx (noticed)"]
