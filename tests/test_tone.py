"""Tone: set from a DM, read fresh into every owner and public turn."""

from pathlib import Path

from quartermaster import agent
from quartermaster.agent import tone
from quartermaster.config import Settings


def test_write_read_and_reset(tmp_path: Path):
    path = tmp_path / "System" / "tone.md"
    assert tone.read(path) == ""
    tone.write(path, "  an angry\n grandma ")
    assert path.read_text("utf-8").startswith("# Tone")
    assert tone.read(path) == "an angry grandma"
    tone.write(path, "")
    assert tone.read(path) == ""


def test_hand_edited_header_still_reads(tmp_path: Path):
    path = tmp_path / "tone.md"
    path.write_text("# Tone\n\npirate, lots of arr\n", "utf-8")
    assert tone.read(path) == "pirate, lots of arr"


def test_prompt_block(tmp_path: Path):
    path = tmp_path / "tone.md"
    assert tone.for_prompt(None) == ""
    assert tone.for_prompt(path) == tone.PROFANITY  # swearing is on even with no tone
    tone.write(path, "an angry grandma")
    block = tone.for_prompt(path)
    assert "an angry grandma" in block and tone.PROFANITY in block


def test_chat_reads_tone_fresh_every_turn_and_the_digest_never(tmp_path: Path):
    settings = Settings(vault=tmp_path)
    owner, public = agent.owner_profile(settings), agent.public_profile(settings)
    assert "set_tone" in agent.OWNER_LIMITS
    assert "pirate" not in agent._options(owner).system_prompt["append"]
    tone.write(tone.tone_path(settings), "a seasick pirate")
    for profile in (owner, public):
        append = agent._options(profile).system_prompt["append"]
        assert "a seasick pirate" in append and tone.PROFANITY in append
    # Chat only: the digest keeps its own voice (the owner asked).
    for profile in (agent.parser_profile(settings, {"type": "object"}),
                    agent.digest_profile(settings, {"type": "object"})):
        assert "pirate" not in agent._options(profile).system_prompt.get("append", "")


def test_set_tone_tool_and_protection(tmp_path: Path, monkeypatch):
    from quartermaster import mcp_servers
    from quartermaster.mcp_servers import qm

    monkeypatch.setattr(mcp_servers, "settings", lambda: Settings(vault=tmp_path))
    monkeypatch.setattr(qm, "settings", lambda: Settings(vault=tmp_path))
    assert "saved" in qm.set_tone("an angry grandma")
    assert tone.read(tmp_path / "System" / "tone.md") == "an angry grandma"
    assert "reset" in qm.set_tone("")
    owner = agent.owner_profile(Settings(vault=tmp_path))
    assert agent.check_tool(owner, "mcp__qm__set_tone", {}) is None
    # Only the tool writes it: an email can't talk the agent into restyling the server.
    assert agent.check_tool(owner, "Write", {"file_path": str(tmp_path / "System" / "tone.md")})
