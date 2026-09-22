"""The dev queue: only the owner's literal `!queue` text lands in it."""

from pathlib import Path

from quartermaster import agent, dev_queue
from quartermaster.config import Settings
from quartermaster.surfaces.discord_bot import Quartermaster


class Channel:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content):
        self.sent.append(content)


def test_add_and_list(tmp_path: Path):
    path = tmp_path / "dev-queue.md"
    assert "empty" in dev_queue.listing(path)
    dev_queue.add(path, "make the   digest\nshorter")
    dev_queue.add(path, "add a /budget command")
    path.write_text(path.read_text().replace("- [ ]", "- [x]", 1))  # owner ticks the first off
    assert [text for _, text in dev_queue.open_items(path)] == ["add a /budget command"]


async def test_queue_command_stores_the_owners_exact_text(tmp_path: Path):
    qm, ch = Quartermaster(Settings(vault=tmp_path)), Channel()
    assert await qm.command(ch, "!queue stop pinging me before 9am")
    assert dev_queue.open_items(dev_queue.queue_path(qm.settings))[0][1] == "stop pinging me before 9am"
    assert await qm.command(ch, "!queue")
    assert "stop pinging me before 9am" in ch.sent[-1]


def test_no_agent_may_write_the_queue(tmp_path: Path):
    # Otherwise an injected email could queue a code change.
    owner = agent.owner_profile(Settings(vault=tmp_path))
    assert agent.check_tool(owner, "Edit", {"file_path": "90-System/dev-queue.md"})
    assert agent.check_tool(owner, "Write", {"file_path": str(tmp_path / "90-System" / "dev-queue.md")})
    assert agent.check_tool(owner, "Read", {"file_path": "90-System/dev-queue.md"}) is None
    assert agent.check_tool(owner, "Write", {"file_path": "90-System/pending.md"}) is None
