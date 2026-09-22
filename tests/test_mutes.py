"""The mute list is the safety valve on every nudge, so it gets real tests."""

from pathlib import Path

from quartermaster import mutes


def test_default_is_to_remind(tmp_path: Path):
    path = tmp_path / "muted.md"
    assert mutes.is_muted("stale:abc", mutes.load(path)) is False


def test_mute_persists_and_is_readable(tmp_path: Path):
    path = tmp_path / "muted.md"
    assert mutes.add(path, "stale:abc", "Old notes page", "don't need it") is True

    loaded = mutes.load(path)
    assert mutes.is_muted("stale:abc", loaded) is True

    text = path.read_text(encoding="utf-8")
    assert "Old notes page" in text
    assert "don't need it" in text, "the reason must survive, so future-you knows why"


def test_muting_twice_is_a_noop(tmp_path: Path):
    path = tmp_path / "muted.md"
    assert mutes.add(path, "stale:abc") is True
    assert mutes.add(path, "stale:abc") is False
    assert len(mutes.load(path)) == 1


def test_mute_covers_nested_scopes(tmp_path: Path):
    path = tmp_path / "muted.md"
    mutes.add(path, "event:artist/Tool")
    loaded = mutes.load(path)

    assert mutes.is_muted("event:artist/Tool/2026-11-02", loaded) is True
    assert mutes.is_muted("event:artist/Tooltip", loaded) is False, (
        "prefix matching must respect the '/' boundary, or muting one artist "
        "would silence every artist whose name starts the same way"
    )


def test_hand_written_content_survives(tmp_path: Path):
    path = tmp_path / "muted.md"
    mutes.add(path, "stale:one")
    path.write_text(path.read_text(encoding="utf-8") + "\nA note I typed myself.\n", encoding="utf-8")

    mutes.add(path, "stale:two")

    assert "A note I typed myself." in path.read_text(encoding="utf-8")
    assert len(mutes.load(path)) == 2

