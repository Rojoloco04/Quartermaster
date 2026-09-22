"""The agent's only Notion write path is scoped in code to one page tree."""

import pytest

from quartermaster.integrations import claude_page
from quartermaster.integrations.notion import NotionError

ROOT = "3e17c599fefe81b3981ccc406d7a7d42"


class FakeClient:
    def __init__(self, parents: dict[str, str]):
        self.parents = parents

    def retrieve_page(self, page_id):
        return {"parent": {"type": "page_id", "page_id": self.parents.get(page_id, "someone-else")}}


def test_default_and_root_target_the_claude_page():
    client = FakeClient({})
    assert claude_page._target(client, ROOT, None) == ROOT
    assert claude_page._target(client, ROOT, "3e17c599-fefe-81b3-981c-cc406d7a7d42") == ROOT


def test_direct_children_are_writable():
    child = "aaaa0000000000000000000000000000"
    assert claude_page._target(FakeClient({child: ROOT}), ROOT, child) == child


def test_anything_else_is_refused():
    with pytest.raises(NotionError, match="isn't under the Claude page"):
        claude_page._target(FakeClient({}), ROOT, "28b7c599fefe80239d81cbb62267f584")
