"""The /brain graph: what becomes a node, what becomes an edge, and what the
note endpoint will and won't serve."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from quartermaster.config import DEFAULTS, Settings
from quartermaster.surfaces import brain, web

NOW = 1_790_000_000.0


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "Vault"

    def note(rel: str, text: str) -> None:
        (v / rel).parent.mkdir(parents=True, exist_ok=True)
        (v / rel).write_text(text, "utf-8")

    note("notion/hobbies-11111111.md",
         '---\ntitle: "Hobbies"\nlast_edited: "2026-09-01T00:00:00.000Z"\nurl: "https://app.notion.com/p/x"\n---\n\n'
         "[Gaming](./hobbies/gaming-22222222.md)\n")
    note("notion/hobbies/gaming-22222222.md", '---\ntitle: "Gaming"\n---\nosu and league\n')
    note("notion/hobbies/cooking-33333333.md", '---\ntitle: "Cooking"\n---\nno links here\n')
    note("facts/interests.md", "# Interests\n\n- Gaming, mostly rhythm games. See [[Cooking]] too.\n")
    note("notion/notes-44444444.md", '---\ntitle: "Notes"\n---\n')
    for i in range(8):  # "Notes" named everywhere: a title that says nothing
        note(f"facts/f{i}.md", f"# Fact {i}\n\nNotes notes notes.\n")
    # Working material, not knowledge: none of these are nodes.
    for rel in ("facts/README.md", "notion/README.md", "digests/2026-09-21.md", "inbox/idea.md",
                "90-System/muted.md", "CLAUDE.md", ".claude/settings.md", "notion-backups/old.md"):
        note(rel, "# Not knowledge\n\n[[Gaming]] Cooking\n")
    return v


def edge_set(graph: dict) -> set[tuple[str, str, str]]:
    return {(e["source"], e["target"], e["kind"]) for e in graph["edges"]}


def test_nodes_are_the_visible_notes(vault):
    graph = brain.build(vault, now=NOW)
    ids = {n["id"] for n in graph["nodes"]}
    assert "notion/hobbies/gaming-22222222.md" in ids
    assert {i.split("/")[0] for i in ids} == {"facts", "notion"}
    assert not any(i.endswith("README.md") for i in ids)
    hobbies = next(n for n in graph["nodes"] if n["id"] == "notion/hobbies-11111111.md")
    assert hobbies["title"] == "Hobbies" and hobbies["group"] == "hobbies"  # the mirror splits by section
    assert {n["group"] for n in graph["nodes"] if n["id"].startswith("notion/")} == {"hobbies", "notion"}
    assert 19 < hobbies["age_days"] < 21  # Notion's last_edited, not the file's mtime


def test_edges_from_links_folders_and_mentions(vault):
    edges = edge_set(brain.build(vault, now=NOW))
    assert ("notion/hobbies-11111111.md", "notion/hobbies/gaming-22222222.md", "link") in edges
    assert ("facts/interests.md", "notion/hobbies/cooking-33333333.md", "link") in edges  # [[wikilink]]
    assert ("notion/hobbies-11111111.md", "notion/hobbies/cooking-33333333.md", "folder") in edges
    assert ("facts/interests.md", "notion/hobbies/gaming-22222222.md", "mention") in edges
    # The folder edge to Gaming is dropped: the link already joins them.
    assert ("notion/hobbies-11111111.md", "notion/hobbies/gaming-22222222.md", "folder") not in edges
    # "Notes" appears in most of the vault, so it links nothing.
    assert not any(t == "notion/notes-44444444.md" for _, t, _ in edges)


def test_agent_reads_and_writes_light_notes_up(vault):
    log = (
        f"x [aa] tool call: Read({{'file_path': '{str(vault / 'facts' / 'interests.md').replace(chr(92), chr(92) * 2)}'}})\n"
        "x [aa] tool call: Edit({'replace_all': False, 'file_path': 'facts/interests.md', 'old_string': 'a'})\n"
        "x [aa] tool call: Read({'file_path': 'C:\\\\elsewhere\\\\secret.md'})\n"
    )
    graph = brain.build(vault, log, now=NOW)
    touched = {n["id"]: n["touched"] for n in graph["nodes"] if n["touched"]}
    assert touched == {"facts/interests.md": 2}


def test_note_path_stays_in_the_vault(vault, tmp_path):
    (tmp_path / "outside.md").write_text("secret", "utf-8")
    assert brain.note_path(vault, "facts/interests.md")
    for bad in ("../outside.md", str(tmp_path / "outside.md"), ".claude/settings.md", "facts", "missing.md",
                "facts/README.md", "digests/2026-09-21.md", "CLAUDE.md"):
        assert brain.note_path(vault, bad) is None, bad


def test_markdown_links():
    html = web.markdown_to_html(
        "[site](https://x.com/a?b=1&c=2) [note](./n.md) [gone](./missing.md) [bad](javascript:alert(1))",
        resolve=lambda target: "n.md" if target == "./n.md" else None,
    )
    assert '<a href="https://x.com/a?b=1&amp;c=2" target="_blank"' in html
    assert '<a href="#" data-note="n.md">note</a>' in html
    assert "gone" in html and "missing.md" not in html
    assert "javascript" not in html


def test_markdown_joins_hard_wrapped_lines():
    html = web.markdown_to_html("One line\nand its wrap.\n\nNext para.\n- a bullet\n  that wraps\n- another\n# Head")
    assert "<p>One line and its wrap.</p>" in html
    assert "<p>Next para.</p>" in html
    assert "<li>a bullet that wraps</li>" in html and "<li>another</li>" in html
    assert "<h2>Head</h2>" in html


def test_endpoints(vault, tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "log_path", property(lambda self: tmp_path / "logs" / "quartermaster.log"))
    (tmp_path / "outside.md").write_text("secret", "utf-8")
    client = TestClient(web.build_app(Settings(vault=vault, prefs=DEFAULTS)), base_url="http://127.0.0.1")
    assert "Brain" in client.get("/brain").text
    graph = client.get("/api/brain").json()
    assert len(graph["nodes"]) == 13

    note = client.get("/api/brain/note", params={"id": "notion/hobbies-11111111.md"}).json()
    assert note["url"] == "https://app.notion.com/p/x"
    assert 'data-note="notion/hobbies/gaming-22222222.md"' in note["html"]
    assert {o["title"] for o in note["out"]} == {"Gaming", "Cooking"}
    assert "title:" not in note["html"]  # frontmatter stripped

    for bad in ("../outside.md", ".claude/settings.md", "inbox/idea.md"):
        assert client.get("/api/brain/note", params={"id": bad}).status_code == 404
