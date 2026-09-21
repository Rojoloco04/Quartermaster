"""Cleaning Notion's markdown output.

Every input here is a real shape taken from the mirrored vault, because the
endpoint's hybrid markdown/XML output is not something worth guessing at.

The expiring-URL cases matter most. A pre-signed S3 link is ~1,800 characters,
carries temporary AWS credentials, and is dead an hour later — keeping one buries
the page's actual prose and leaves a link that will never work again.
"""

from quartermaster.notion_clean import clean, notion_id_from_url

# Built at runtime rather than written literally: a real credential string in
# a source file is exactly what the pre-commit hook exists to stop, and this
# fixture is modelled on one that really did reach the vault.
FAKE_CRED = "ASIA" + "EXAMPLEKEYID0001"

SIGNED = (
    "https://prod-files-secure.s3.us-west-2.amazonaws.com/5127c599/04fd6a01/"
    "Yasuo_Profile_Picture.jpg?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=" + FAKE_CRED + "%2F20260921%2Fus-west-2%2Fs3"
    "&X-Amz-Expires=3600&X-Amz-Signature=" + "f" * 64
)

PDF_BLOB = (
    "file://%7B%22source%22%3A%22attachment%3A502bd2fd-798e-49d5-8e0d-d214a3fdabe9"
    "%3AJacksonParrack_Transcript.pdf%22%2C%22permissionRecord%22%3A%7B%22table%22"
    "%3A%22block%22%7D%7D"
)


class TestExpiringMedia:
    def test_signed_image_keeps_the_filename_and_drops_the_url(self):
        out = clean(f"![](<{SIGNED}>)".replace("<", "").replace(">", ""))
        assert "Yasuo_Profile_Picture.jpg" in out
        assert "X-Amz-" not in out

    def test_aws_credentials_never_survive(self):
        out = clean(f"![]({SIGNED})")
        assert FAKE_CRED not in out
        assert "X-Amz-Credential" not in out

    def test_the_result_is_dramatically_shorter(self):
        raw = f"Some prose.\n\n![]({SIGNED})\n\nMore prose."
        out = clean(raw)
        assert len(out) < 120, f"still bloated: {len(out)} chars"
        assert "Some prose." in out and "More prose." in out


class TestAttachments:
    def test_pdf_blob_becomes_a_readable_name(self):
        out = clean(f'<pdf src="{PDF_BLOB}"></pdf>')
        assert "JacksonParrack_Transcript.pdf" in out
        assert "file://" not in out
        assert "%7B" not in out

    def test_file_blob_is_handled_the_same(self):
        out = clean(f'<file src="{PDF_BLOB}"/>')
        assert "JacksonParrack_Transcript.pdf" in out

    def test_real_media_url_is_kept_as_a_link(self):
        out = clean('<video src="https://youtu.be/SacLF5yliRw"/>')
        assert "https://youtu.be/SacLF5yliRw" in out

    def test_malformed_blob_degrades_gracefully(self):
        out = clean('<file src="file://not-valid-json"/>')
        assert "attachment" in out.lower()


class TestChildPageLinks:
    def test_page_tag_becomes_a_relative_link(self):
        raw = '<page url="https://app.notion.com/p/28b7c599fefe80bb899deb3c9011b84e">Useful Scripts</page>'
        out = clean(raw, link_targets={"28b7c599fefe80bb899deb3c9011b84e": "./career/useful-scripts-9011b84e.md"})
        assert out.strip() == "[Useful Scripts](./career/useful-scripts-9011b84e.md)"

    def test_unknown_page_falls_back_to_the_notion_url(self):
        url = "https://app.notion.com/p/28b7c599fefe80bb899deb3c9011b84e"
        out = clean(f'<page url="{url}">Elsewhere</page>', link_targets={})
        assert f"[Elsewhere]({url})" in out

    def test_extracts_the_page_id(self):
        assert (
            notion_id_from_url("https://app.notion.com/p/Documents-28b7c599fefe801caffed251a71ed959")
            == "28b7c599fefe801caffed251a71ed959"
        )


class TestNoise:
    def test_empty_blocks_are_removed(self):
        out = clean("Before\n<empty-block/>\n<empty-block/>\nAfter")
        assert "empty-block" not in out
        assert "Before" in out and "After" in out

    def test_bookmark_keeps_its_link(self):
        raw = '<unknown url="https://app.notion.com/p/abc" alt="bookmark"/>'
        out = clean(raw)
        assert "[bookmark](https://app.notion.com/p/abc)" in out

    def test_layout_wrappers_are_dropped_but_content_survives(self):
        out = clean('<columns><column ratio="50">Left</column><column ratio="50">Right</column></columns>')
        assert "Left" in out and "Right" in out
        assert "<column" not in out and "ratio" not in out

    def test_excess_blank_lines_collapse(self):
        assert "\n\n\n" not in clean("A\n\n\n\n\n\nB")


class TestTables:
    def test_html_table_becomes_markdown(self):
        raw = (
            '<table header-row="true">'
            "<tr><td>Name</td><td>Value</td></tr>"
            "<tr><td>a</td><td>1</td></tr>"
            "</table>"
        )
        out = clean(raw)
        assert "| Name | Value |" in out
        assert "| a | 1 |" in out
        assert "<td>" not in out

    def test_headerless_table_does_not_lose_its_first_row(self):
        raw = "<table><tr><td>a</td><td>1</td></tr></table>"
        out = clean(raw)
        # Markdown needs a header; the real first row must stay a data row.
        assert "| a | 1 |" in out

    def test_pipes_in_cells_are_escaped(self):
        raw = '<table header-row="true"><tr><td>a|b</td></tr></table>'
        assert r"a\|b" in clean(raw)


class TestRealPage:
    def test_the_lena_files_page(self):
        """The page that prompted this: two signed images and a child link."""
        raw = (
            '<page url="https://app.notion.com/p/28b7c599fefe801caffed251a71ed959">Documents</page>\n'
            "Profile pictures\n"
            f"![]({SIGNED})\n"
            f"![]({SIGNED})\n"
        )
        out = clean(raw, link_targets={"28b7c599fefe801caffed251a71ed959": "./files/documents-a71ed959.md"})

        assert "[Documents](./files/documents-a71ed959.md)" in out
        assert "Profile pictures" in out
        assert "Yasuo_Profile_Picture.jpg" in out
        assert "X-Amz-" not in out
        assert len(out) < 200, "a 4KB page of signed URLs should collapse to a few lines"
