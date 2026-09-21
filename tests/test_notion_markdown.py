"""Reading page content out of the markdown endpoint.

These exist because of a real bug. The docs describe a `page_markdown` field,
which I read as the content key — it is actually the response's `object` type
discriminator. The content sits at the top level under `markdown`.

The consequence was worse than a wrong key: extraction returned "", the sync
wrote 79 frontmatter-only files, and reported "79 written" with no warning. The
mirror looked healthy and contained nothing. So these tests pin both the real
shape and the refusal to treat unreadable content as an empty page.
"""

import pytest

from quartermaster.integrations.notion import MarkdownShapeError, extract_markdown


def test_reads_the_real_api_shape():
    # Captured verbatim from GET /v1/pages/{id}/markdown.
    response = {
        "object": "page_markdown",
        "id": "3e17c599-fefe-8123-9804-dc1a41dec58d",
        "markdown": "Adam's kit, ordered 9/20.\n**Check before you order:** ...",
        "truncated": False,
        "unknown_block_ids": [],
        "request_id": "a2c51fbb-ed9d-4f10-b893-0a666307af6d",
    }
    assert extract_markdown(response).startswith("Adam's kit")


def test_object_discriminator_is_not_mistaken_for_content():
    # The bug in one assertion: 'page_markdown' as a *value* of `object` must
    # never be treated as page text.
    response = {"object": "page_markdown", "id": "abc", "markdown": "real body"}
    assert extract_markdown(response) == "real body"


def test_genuinely_empty_page_returns_empty():
    response = {"object": "page_markdown", "id": "abc", "markdown": "", "truncated": False}
    assert extract_markdown(response) == ""


def test_whitespace_only_page_counts_as_empty():
    response = {"object": "page_markdown", "id": "abc", "markdown": "   \n\n  "}
    assert extract_markdown(response) == ""


def test_unknown_content_key_raises_rather_than_returning_empty():
    # The whole point. If the API moves the content somewhere we don't read,
    # we must fail loudly instead of mirroring a blank page.
    response = {
        "object": "page_markdown",
        "id": "abc",
        "body_text": "A page's worth of real content that we failed to recognise, " * 3,
    }
    with pytest.raises(MarkdownShapeError, match="body_text"):
        extract_markdown(response)


def test_metadata_strings_do_not_trigger_a_false_alarm():
    # Long ids and urls are metadata; they must not be mistaken for content.
    response = {
        "object": "page_markdown",
        "id": "3e17c599-fefe-8123-9804-dc1a41dec58d",
        "url": "https://app.notion.com/p/" + "x" * 120,
        "request_id": "a" * 100,
        "markdown": "",
    }
    assert extract_markdown(response) == ""


def test_tolerates_a_nested_object_shape():
    response = {"object": "page_markdown", "markdown": {"content": "nested body"}}
    assert extract_markdown(response) == "nested body"
