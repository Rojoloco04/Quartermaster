"""The resume: resume.tex read in code, the site generated from it, and the
publish that keeps the PDF, the site and the source in one commit. Git runs for
real against a bare remote in a temp dir; Tectonic is faked (it needs a
download and a TeX bundle), except in the opt-in build test at the end."""

import json
import subprocess
from pathlib import Path

import pytest

from quartermaster import db
from quartermaster.config import DEFAULTS, Settings
from quartermaster.resume import build, proposals, publish, site, tex
from quartermaster.resume.tex import ResumeError


TEX = r"""\documentclass{resume}
\begin{document}
\header{Ann Example}{+1 555 \sep \link{mailto:a@example.com}{a@example.com}}

\section{Education}
\school{uni}{Some University}{Town, ST}{Aug. 2020 - May 2024}{B.S. Things}{GPA: 4.0/4.0}

\section{Experience}
\job{acme}{Acme}{Town, ST}{Engineer}{Jan. 2025 - Present}
\begin{bullets}
  \item Built a \textasciitilde\$5 widget for 10\% of R\&D.
  % a comment between items
  \item Won 1\textsuperscript{st} place.
\end{bullets}

\section{Projects}
\project{gizmo}{The Gizmo}{Best Gizmo}{\link{https://github.com/a/gizmo}{GitHub}}{2024}{A gizmo that $\sim$works}
\begin{bullets}
  \item Wrote it in C.
\end{bullets}

\section{Leadership}
\role{club}{Gizmo Club}{President}{Aug. 2022 - May 2024}

\section{Skills}
\skills{Languages}{C, Python}
\skills{Spoken}{Spanish (Advanced)}
\end{document}
"""

SITE = """[site]
index = "index.html"
pdf = "Ann_Resume.pdf"

[entries.acme]
logo = "images/acme.png"
url = "https://acme.example"
meta = "Remote · {location}"
tags = ["C", "Python"]

[entries.uni]
activities = [{ role = "club" }, { text = "Chess & Go", dates = "2023" }]

[projects]
order = ["gizmo", "side"]

[entries.gizmo]
title = "Gizmo"
blurb = "Site words about the gizmo."

[entries.side]
site_only = true
title = "Side Project"
url = "https://github.com/a/side"
tagline = "Just for fun"

[skills.levels]
C = 4
Python = 2
Spanish = 3
"""

HTML = """<html>\r
<div>\r
            <!-- resume:projects: generated -->\r
            old\r
            <!-- /resume:projects -->\r
</div>\r
<div>\r
            <!-- resume:experience -->\r
            <!-- /resume:experience -->\r
</div>\r
<div>\r
            <!-- resume:education -->\r
            <!-- /resume:education -->\r
</div>\r
<div>\r
            <!-- resume:skills -->\r
            <!-- /resume:skills -->\r
</div>\r
<p>hand-made</p>\r
</html>\r
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def parsed() -> tex.Resume:
    return tex.parse(TEX)


# --- Reading resume.tex ---------------------------------------------------------------


def test_parse_reads_every_entry_bullet_and_skill():
    r = parsed()
    assert r.name == "Ann Example"
    assert [(e.kind, e.id, e.section) for e in r.entries] == [
        ("school", "uni", "Education"), ("job", "acme", "Experience"),
        ("project", "gizmo", "Projects"), ("role", "club", "Leadership")]
    acme = r.by_id()["acme"]
    assert acme.text("role") == "Engineer"
    assert acme.bullet_texts() == ["Built a ~$5 widget for 10% of R&D.", "Won 1st place."]
    assert r.by_id()["gizmo"].text("summary") == "A gizmo that ~works"
    assert [(g.category, g.items) for g in r.skills] == [
        ("Languages", [("C", ""), ("Python", "")]), ("Spoken", [("Spanish", "Advanced")])]


def test_inline_html_keeps_markup_and_escapes():
    assert tex.to_html(r"1\textsuperscript{st} R\&D <x>") == "1<sup>st</sup> R&amp;D &lt;x&gt;"
    assert tex.to_html(r"\textbf{big} \href{https://x.example}{here}") == \
        '<strong>big</strong> <a href="https://x.example">here</a>'
    assert tex.to_text("Aug. 2020 -- May 2024") == "Aug. 2020 \u2013 May 2024"


@pytest.mark.parametrize("body, message", [
    (r"\vspace{2pt}", r"\vspace isn't part"),
    ("stray words", "text outside any entry"),
    (r"\job{acme}{A}{B}{C}{D}", "before any \\section"),
    (r"\section{X}\job{a}{A}{B}{C}{D}\job{a}{A}{B}{C}{D}", "duplicate id"),
    (r"\section{X}\job{Bad_Id}{A}{B}{C}{D}", "use lowercase"),
    (r"\section{X}\job{a}{A}{B}{\fancy{C}}{D}", r"\fancy isn't allowed"),
    (r"\section{X}\job{a}{A}{B}{C}{D}\begin{bullets}\item $x^2$\end{bullets}", "math mode"),
    (r"\begin{bullets}\item x\end{bullets}", "before any entry"),
])
def test_anything_outside_the_vocabulary_is_an_error_not_skipped(body, message):
    source = "\\begin{document}\\header{N}{c}\n" + body + "\n\\end{document}"
    with pytest.raises(ResumeError, match=message.replace("\\", "\\\\").replace("(", "\\(")):
        tex.parse(source)


def test_errors_name_the_line():
    source = "\\begin{document}\n\\header{N}{c}\n\n\\oops\n\\end{document}"
    with pytest.raises(ResumeError, match="line 4"):
        tex.parse(source)


def test_diff_shows_changed_bullets_fields_and_skills():
    new = TEX.replace("Won 1\\textsuperscript{st} place.", "Won 2nd place.") \
             .replace("{Engineer}", "{Senior Engineer}").replace("{C, Python}", "{C, Python, Go}")
    changes = {c.id: c for c in tex.diff(parsed(), tex.parse(new))}
    assert changes["acme"].lines == ["- role: Engineer", "+ role: Senior Engineer",
                                     "- • Won 1st place.", "+ • Won 2nd place."]
    assert changes["skills"].lines == ["- Languages: C, Python", "+ Languages: C, Python, Go"]
    assert set(changes) == {"acme", "skills"}


# --- The site ------------------------------------------------------------------------------


def test_render_quotes_the_resume_and_adds_extras():
    regions = site.render(parsed(), site.load_extras(SITE))
    exp = regions["experience"]
    assert "<h3 class=\"font-medium text-[17px] leading-tight\">Engineer</h3>" in exp
    assert "Remote · Town, ST" in exp
    assert "Jan. 2025 \u2013 Present" in exp  # site dates use an en dash
    assert "<li>Built a ~$5 widget for 10% of R&amp;D.</li>" in exp
    assert "<li>Won 1<sup>st</sup> place.</li>" in exp
    assert "C · Python" in exp
    edu = regions["education"]
    assert "President · Gizmo Club" in edu and "Aug. 2022 \u2013 May 2024" in edu
    assert "Chess &amp; Go" in edu
    projects = regions["projects"]
    assert projects.index("Gizmo") < projects.index("Side Project")
    assert 'href="https://github.com/a/gizmo"' in projects  # the link comes from the resume
    assert "Best Gizmo" in projects and "Site words about the gizmo." in projects
    assert 'data-level="4">C</li>' in regions["skills"]


def test_unknown_placeholder_is_an_error():
    extras = site.load_extras(SITE.replace("Remote · {location}", "Remote · {city}"))
    with pytest.raises(ResumeError, match=r"entries.acme.meta: \{city\}"):
        site.render(parsed(), extras)


@pytest.mark.parametrize("change, problem", [
    ('[entries.ghost]\nlogo = "x"\n', "entries.ghost but resume.tex has no entry"),
    (None, "resume project 'gizmo' isn't on the site"),
    ("", "resume role 'club' isn't on the site"),
    ("", "skill 'Python' has no level"),
])
def test_structure_problems(change, problem):
    text = SITE
    if problem.startswith("resume project"):
        text = text.replace('order = ["gizmo", "side"]', 'order = ["side"]')
    elif problem.startswith("resume role"):
        text = text.replace('{ role = "club" }, ', "")
    elif problem.startswith("skill"):
        text = text.replace("Python = 2\n", "")
    else:
        text += change
    assert any(problem in p for p in site.structure_problems(parsed(), site.load_extras(text)))


def test_site_text_goes_stale_when_its_entry_changes_until_reviewed():
    r, extras = parsed(), site.load_extras(SITE)
    # acme and gizmo have their own words on the site; uni has only role-linked activities (also claims).
    assert {e.id for e in site.stale(r, extras, {})} == {"acme", "uni", "gizmo"}
    reviewed = json.loads(site.reviewed_json(r, extras, {}, {"acme", "uni", "gizmo"}))
    assert site.stale(r, extras, reviewed) == []
    changed = tex.parse(TEX.replace("Wrote it in C.", "Wrote it in Rust."))
    assert [e.id for e in site.stale(changed, extras, reviewed)] == ["gizmo"]


def test_regions_replace_only_marked_html():
    regions = site.render(parsed(), site.load_extras(SITE))
    html = HTML.replace("\r\n", "\n")
    out = site.apply_regions(html, regions)
    assert "old" not in out and "<p>hand-made</p>" in out
    assert site.current_region(out, "experience") == regions["experience"]
    assert site.apply_regions(out, regions) == out  # stable


def test_missing_markers_are_an_error():
    with pytest.raises(ResumeError, match="resume:projects"):
        site.apply_regions("<html></html>", {name: "" for name in site.REGIONS})


# --- PDF text check ------------------------------------------------------------------------


def test_missing_text_finds_a_dropped_bullet_and_tolerates_layout():
    r = parsed()
    good = ("Ann Example\nSome University | Town, ST Aug. 2020 - May 2024\nB.S. Things GPA: 4.0/4.0\n"
            "Acme | Town, ST\nEngineer Jan. 2025 - Present\n• Built a ˜$5 widget for 10% of\nR&D.\n"
            "• Won 1\nst place.\nThe Gizmo | Best Gizmo | GitHub 2024\nA gizmo that ~works\n• Wrote it in C.\n"
            "Gizmo Club | President Aug. 2022 - May 2024\nLanguages: C, Python\nSpoken: Spanish (Advanced)")
    assert build.missing_text(r, good) == []
    assert build.missing_text(r, good.replace("• Won 1\nst place.\n", "")) == ["Won 1st place."]


# --- Edits proposed from a DM -------------------------------------------------------------


def test_edits_must_hit_exactly_one_place():
    files = {"resume.tex": "a b a", "site.toml": "x"}
    assert proposals.edited(files, [{"file": "resume.tex", "old": "b", "new": "c"}])["resume.tex"] == "a c a"
    with pytest.raises(ResumeError, match="appears 2 times"):
        proposals.edited(files, [{"old": "a", "new": "z"}])
    with pytest.raises(ResumeError, match="appears 0 times"):
        proposals.edited(files, [{"old": "nope", "new": "z"}])
    with pytest.raises(ResumeError, match="change nothing"):
        proposals.edited(files, [{"old": "b", "new": "b"}])
    with pytest.raises(ResumeError, match="file must be"):
        proposals.edited(files, [{"file": "index.html", "old": "b", "new": "c"}])


# --- Publishing, against real git -----------------------------------------------------------


@pytest.fixture
def portfolio(tmp_path: Path, monkeypatch) -> Path:
    remote, root = tmp_path / "remote.git", tmp_path / "site"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "init", "-q", "-b", "main", str(root))
    git(root, "config", "user.name", "t")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    git(root, "config", "core.autocrlf", "false")
    (root / "resume").mkdir()
    (root / "resume" / "resume.tex").write_text(TEX, encoding="utf-8")
    (root / "resume" / "resume.cls").write_text("% layout\n", encoding="utf-8")
    (root / "resume" / "site.toml").write_text(SITE, encoding="utf-8")
    (root / "index.html").write_bytes(HTML.encode("utf-8"))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-q", "-u", "origin", "main")

    def fake_build(resume, tex_dir, source=None):
        lines = [resume.name]
        for e in resume.entries:  # in reading order, like the real PDF
            lines += [e.text(k) for k in e.fields] + e.bullet_texts()
        text = "\n".join(lines + [f"{g.category}:" for g in resume.skills])
        return build.Built(pdf=b"%PDF " + text.encode("utf-8"), pages=1, text=text)

    monkeypatch.setattr(build, "build", fake_build)
    monkeypatch.setattr(build, "pdf_text", lambda pdf: (1, pdf.decode("utf-8")[5:]))
    return root


@pytest.fixture
def settings(portfolio: Path, tmp_path: Path) -> Settings:
    prefs = {**DEFAULTS, "resume": {"repo": str(portfolio)}}
    return Settings(vault=tmp_path / "Vault", prefs=prefs)


def remote_log(root: Path) -> list[str]:
    return git(root.parent / "remote.git", "log", "--format=%s", "main").splitlines()


def test_publish_refuses_unreviewed_site_text_then_publishes_all_in_one_commit(settings, portfolio):
    with pytest.raises(ResumeError, match="acme: meta: Remote · Town, ST"):
        publish.publish(settings)
    assert len(remote_log(portfolio)) == 1

    result = publish.publish(settings, review_all=True, message="first publish")
    assert result.startswith("Published")
    assert remote_log(portfolio)[0] == "Resume: first publish"
    files = git(portfolio, "show", "--name-only", "--format=", "HEAD").split()
    assert set(files) == {"Ann_Resume.pdf", "index.html", "resume/reviewed.json"}
    html = (portfolio / "index.html").read_bytes()
    assert b"\r\n" in html and b"\n" not in html.replace(b"\r\n", b"")  # line ends kept
    assert publish.check(settings) == []
    assert publish.publish(settings).startswith("Nothing to publish")


def test_a_changed_bullet_blocks_until_its_site_text_is_reviewed(settings, portfolio):
    publish.publish(settings, review_all=True)
    tex_path = portfolio / "resume" / "resume.tex"
    tex_path.write_text(TEX.replace("Won 1\\textsuperscript{st} place.", "Won 2nd place."), encoding="utf-8")
    assert any(p.startswith("acme: its site text") for p in publish.check(settings))
    with pytest.raises(ResumeError, match="acme"):
        publish.publish(settings)
    result = publish.publish(settings, reviewed={"acme"})
    assert "acme changed" in result
    assert "Won 2nd place." in (portfolio / "index.html").read_text(encoding="utf-8")
    assert publish.check(settings) == []


def test_a_proposal_is_previewed_then_applied_on_confirm(settings, portfolio, tmp_path):
    publish.publish(settings, review_all=True)
    edits = [{"file": "resume.tex", "old": "Wrote it in C.", "new": "Wrote it in C and Rust."},
             {"file": "site.toml", "old": "Site words about the gizmo.", "new": "A gizmo, now in Rust."}]
    with db.session(tmp_path / "state.db") as conn:
        change_id = proposals.propose(settings, conn, edits, why="Rust port")
        row = conn.execute("SELECT * FROM pending_writes WHERE id = ?", (change_id,)).fetchone()
        assert row["mode"] == "resume" and row["status"] == "pending"
        # Nothing is written until Confirm.
        assert "Rust" not in (portfolio / "resume" / "resume.tex").read_text(encoding="utf-8")

        text = proposals.preview(row, settings)
        assert "- • Wrote it in C." in text and "+ • Wrote it in C and Rust." in text
        assert "+blurb = \"A gizmo, now in Rust.\"" in text
        assert "gizmo: title: Gizmo / blurb: A gizmo, now in Rust." in text  # the site text being vouched for

        assert proposals.apply(settings, conn, row).startswith("✅ Published")
        status = conn.execute("SELECT status FROM pending_writes WHERE id = ?", (change_id,)).fetchone()[0]
    assert status == "applied"
    assert remote_log(portfolio)[0] == "Resume: Rust port"
    assert "A gizmo, now in Rust." in (portfolio / "index.html").read_text(encoding="utf-8")
    assert publish.check(settings) == []


def test_a_proposal_whose_file_changed_since_fails_and_writes_nothing(settings, portfolio, tmp_path):
    publish.publish(settings, review_all=True)
    with db.session(tmp_path / "state.db") as conn:
        change_id = proposals.propose(settings, conn, [{"old": "Wrote it in C.", "new": "Wrote it."}], why="x")
        row = conn.execute("SELECT * FROM pending_writes WHERE id = ?", (change_id,)).fetchone()
        tex_path = portfolio / "resume" / "resume.tex"
        tex_path.write_text(TEX.replace("Some University", "Other University"), encoding="utf-8")
        git(portfolio, "commit", "-q", "-am", "edited by hand")
        assert "changed since this was proposed" in proposals.apply(settings, conn, row)
    assert "Wrote it in C." in tex_path.read_text(encoding="utf-8")


def test_a_proposal_that_breaks_the_resume_is_refused_up_front(settings, tmp_path):
    with db.session(tmp_path / "state.db") as conn, pytest.raises(ResumeError, match=r"\\vspace"):
        proposals.propose(settings, conn, [{"old": "\\section{Skills}", "new": "\\vspace{1pt}\\section{Skills}"}])


# --- The real thing, when Tectonic is installed ---------------------------------------------


@pytest.mark.skipif(not build.tectonic_path().exists(), reason="Tectonic not installed (qm resume setup)")
def test_real_build_of_the_portfolio_resume_is_one_readable_page():
    from quartermaster.config import load_settings

    try:
        r = publish.repo(load_settings())
    except RuntimeError:
        pytest.skip("no resume.repo configured")
    built = build.build(tex.parse(r.tex.read_text(encoding="utf-8")), r.tex_dir)
    assert built.pages == 1


def test_preview_writes_the_pdf_and_site_without_committing(settings, portfolio):
    publish.publish(settings, review_all=True)
    head = git(portfolio, "rev-parse", "HEAD")
    (portfolio / "resume" / "resume.tex").write_text(TEX.replace("Wrote it in C.", "Wrote it in Zig."),
                                                      encoding="utf-8")
    out = publish.preview(settings)
    assert "gizmo: title: Gizmo" in out  # unreviewed text is listed, not a refusal
    assert "Wrote it in Zig." in (portfolio / "Ann_Resume.pdf").read_bytes().decode("utf-8")
    assert git(portfolio, "rev-parse", "HEAD") == head
    assert publish.publish(settings, reviewed={"gizmo"}).startswith("Published")
