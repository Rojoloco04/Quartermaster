"""Streaming a turn into Discord: text goes out as written, status stays last."""

from quartermaster.surfaces.discord_bot import LiveStatus, describe_tool


class FakeMessage:
    def __init__(self, channel, content):
        self.channel, self.content, self.deleted = channel, content, False

    async def edit(self, content):
        self.content = content

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self):
        self.sent: list[FakeMessage] = []

    async def send(self, content):
        msg = FakeMessage(self, content)
        self.sent.append(msg)
        return msg

    def visible(self):
        return [m.content for m in self.sent if not m.deleted]


async def test_text_streams_and_status_moves_below_it():
    ch = FakeChannel()
    status = LiveStatus(ch)
    await status.start()
    await status.update("text", "Let me check your calendar.")
    await status.update("text", "You're free Friday.")
    assert ch.visible() == ["Let me check your calendar.", "You're free Friday.", LiveStatus.THINKING]
    await status.done()
    assert ch.visible() == ["Let me check your calendar.", "You're free Friday."]
    assert status.sent_text


async def test_tool_calls_update_the_status_line():
    ch = FakeChannel()
    status = LiveStatus(ch)
    await status.start()
    status._last_edit = 0  # skip the throttle
    await status.update("tool", ("mcp__google__list_events", {}))
    assert ch.visible() == ["🔧 Checking your calendar…"]


def test_describe_tool():
    assert describe_tool("Read", {"file_path": "C:/v/facts/interests.md"}) == "Reading `interests.md`"
    assert describe_tool("WebFetch", {"url": "https://example.com/a"}) == "Reading example.com"
    assert describe_tool("mcp__google__search_email", {}) == "Checking email"
    assert describe_tool("mcp__microsoft__list_tasks", {}) == "Checking To Do"
