"""``qm resume``: check, build and publish the resume and the site together.

Everything lives in the portfolio repo (``resume.repo`` in config.toml):
``resume/resume.tex`` (the source of truth), ``resume/resume.cls`` (layout),
``resume/site.toml`` (what the site adds), ``resume/reviewed.json`` (written
here), the PDF at the repo root and ``index.html``. A publish writes the PDF
and the site's generated sections from the source and commits all of them in
one commit, so no commit ever has a PDF, a site and a source that disagree.

Git the same way the vault push does it: never forces, never skips hooks,
fails rather than prompts for a credential, and refuses a detached HEAD or a
merge in progress. It commits only its own paths; anything else uncommitted in
the repo is left alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..ops.vault_push import PUSH_TIMEOUT_SECONDS, _git, _ok
from . import build, site, tex
from .tex import ResumeError


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Repo:
    root: Path

    @property
    def tex_dir(self) -> Path:
        return self.root / "resume"

    @property
    def tex(self) -> Path:
        return self.tex_dir / "resume.tex"

    @property
    def site_toml(self) -> Path:
        return self.tex_dir / site.SITE_FILE

    @property
    def reviewed(self) -> Path:
        return self.tex_dir / site.REVIEWED_FILE

    def extras(self) -> site.Extras:
        return site.load_extras(self.site_toml.read_text(encoding="utf-8"))

    def paths(self, extras: site.Extras) -> list[str]:
        """Everything a publish writes or commits, relative to the repo."""
        return ["resume/resume.tex", "resume/resume.cls", f"resume/{site.SITE_FILE}",
                f"resume/{site.REVIEWED_FILE}", extras.index, extras.pdf]


def repo(settings: Settings) -> Repo:
    raw = (settings.prefs.get("resume", {}).get("repo") or "").strip()
    if not raw:
        raise ResumeError("No resume repo configured: set resume.repo in the vault's System/config.toml "
                          "to the portfolio repo's folder.")
    root = Path(raw).expanduser()
    if not (root / "resume" / "resume.tex").exists():
        raise ResumeError(f"{root} has no resume/resume.tex")
    return Repo(root)


def read_text(path: Path) -> tuple[str, str]:
    """A file's text with \\n line ends, and the line end it had (kept on write)."""
    raw = path.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), ("\r\n" if "\r\n" in raw else "\n")


def write_text(path: Path, text: str, newline: str = "\n") -> None:
    tmp = path.with_name(path.name + ".partial")
    tmp.write_bytes(text.replace("\n", newline).encode("utf-8"))
    tmp.replace(path)


def _dirty(r: Repo, paths: list[str]) -> list[str]:
    out = _ok(r.root, "status", "--porcelain", "-z", "--", *paths)
    return [item[3:] for item in out.split("\0") if item]


# --- Checking -------------------------------------------------------------------------


def check(settings: Settings) -> list[str]:
    """Everything that would make the site or the PDF disagree with
    resume.tex, without building anything. Empty means in sync."""
    r = repo(settings)
    try:
        resume = tex.parse(r.tex.read_text(encoding="utf-8"))
        extras = r.extras()
    except ResumeError as exc:
        return [str(exc)]
    problems = site.structure_problems(resume, extras)
    structure_ok = not problems
    for entry in site.stale(resume, extras, site.load_reviewed(r.reviewed)):
        problems.append(f"{entry.id}: its site text hasn't been checked against this version of the entry: "
                        + " / ".join(site.site_text(entry, extras)))
    if structure_ok:
        html, _ = read_text(r.root / extras.index)
        regions = site.render(resume, extras)
        for name in site.REGIONS:
            try:
                if site.current_region(html, name) != regions[name]:
                    problems.append(f"the site's {name} section doesn't match resume.tex + site.toml "
                                    "(hand-edited, or not published since a change)")
            except ResumeError as exc:
                problems.append(str(exc))
    pdf = r.root / extras.pdf
    if not pdf.exists():
        problems.append(f"{extras.pdf} doesn't exist yet")
    else:
        pages, text = build.pdf_text(pdf.read_bytes())
        missing = build.missing_text(resume, text)
        if missing:
            problems.append(f"{extras.pdf} doesn't say what resume.tex says (first difference: {missing[0][:60]!r})")
        if pages > build.MAX_PAGES:
            problems.append(f"{extras.pdf} is {pages} pages")
    try:
        dirty = _dirty(r, ["resume/resume.tex", "resume/resume.cls", f"resume/{site.SITE_FILE}"])
    except RuntimeError:
        dirty = []
    if dirty:
        problems.append("unpublished changes in " + ", ".join(dirty))
    return problems


# --- Publishing ----------------------------------------------------------------------


def _preflight(r: Repo) -> None:
    git_dir = r.root / ".git"
    if not git_dir.is_dir():
        raise ResumeError(f"{r.root} is not a git repository")
    for marker in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"):
        if (git_dir / marker).exists():
            raise ResumeError(f"a git operation is in progress in {r.root.name} ({marker}); finish it by hand")
    if _git(r.root, "symbolic-ref", "-q", "HEAD").returncode != 0:
        raise ResumeError(f"{r.root.name}'s HEAD is detached; check out a branch by hand")


def catch_up(r: Repo) -> None:
    """Fast-forward to the remote first (the site may have been edited on
    GitHub), so the push isn't rejected. Never merges or rebases."""
    if _git(r.root, "rev-parse", "--abbrev-ref", "@{u}").returncode != 0:
        return
    _ok(r.root, "fetch", "-q", timeout=PUSH_TIMEOUT_SECONDS)
    behind = _ok(r.root, "rev-list", "--count", "HEAD..@{u}").strip()
    if behind and int(behind):
        result = _git(r.root, "merge", "--ff-only", "-q", "@{u}")
        if result.returncode != 0:
            raise ResumeError(f"{r.root.name} is behind its remote and can't fast-forward "
                              f"({(result.stderr or result.stdout).strip()}); sort it out by hand")


def committed_resume(r: Repo) -> tex.Resume | None:
    result = _git(r.root, "show", "HEAD:resume/resume.tex")
    if result.returncode != 0:
        return None
    try:
        return tex.parse(result.stdout)
    except ResumeError:
        return None


def summary(old: tex.Resume | None, new: tex.Resume) -> str:
    if old is None:
        return "resume.tex added"
    changes = tex.diff(old, new)
    return ", ".join(f"{c.id} {c.kind}" for c in changes) or "layout or site text"


def preview(settings: Settings, *, open_files: bool = False) -> str:
    """Build the PDF and regenerate the site's sections in place, without
    committing: for iterating on a change and looking at it before publishing.
    Unreviewed site text doesn't block a preview; it's listed instead."""
    r = repo(settings)
    resume = tex.parse(read_text(r.tex)[0])
    extras = r.extras()
    regions = site.render(resume, extras)
    built = build.build(resume, r.tex_dir)
    pdf_path, index = r.root / extras.pdf, r.root / extras.index
    pdf_path.write_bytes(built.pdf)
    html, newline = read_text(index)
    write_text(index, site.apply_regions(html, regions), newline)
    lines = [f"Preview written (not committed): {pdf_path} and {index}."]
    pending = site.stale(resume, extras, site.load_reviewed(r.reviewed))
    if pending:
        lines.append("Site text to re-check before publishing:")
        lines += [f"  {e.id}: " + " / ".join(site.site_text(e, extras)) for e in pending]
    if open_files:
        import os

        for path in (pdf_path, index):
            os.startfile(path)  # noqa: S606 - the owner's own files, in their default apps
    return "\n".join(lines)


def publish(settings: Settings, *, reviewed: set[str] | None = None, review_all: bool = False,
            message: str = "", push: bool = True) -> str:
    """Build the PDF, regenerate the site's resume sections, commit and push.

    Refuses while an entry's site text hasn't been re-checked since the
    entry changed, unless its id is in ``reviewed`` (or ``review_all``): the
    caller vouches that the text in site.toml is still right.
    """
    r = repo(settings)
    _preflight(r)
    catch_up(r)
    source, _ = read_text(r.tex)
    resume = tex.parse(source)
    extras = r.extras()
    regions = site.render(resume, extras)

    previous = site.load_reviewed(r.reviewed)
    pending = site.stale(resume, extras, previous)
    ids = {e.id for e in pending} if review_all else (reviewed or set())
    unreviewed = [e for e in pending if e.id not in ids]
    if unreviewed:
        lines = [f"  {e.id}: " + " / ".join(site.site_text(e, extras)) for e in unreviewed]
        raise ResumeError(
            "These changed in resume.tex since their site text was last checked:\n" + "\n".join(lines)
            + "\nFix that text in resume/site.toml if it's wrong, then publish with --reviewed.")

    built = build.build(resume, r.tex_dir)
    (r.root / extras.pdf).write_bytes(built.pdf)  # same source, same bytes (build.SOURCE_DATE_EPOCH)
    index = r.root / extras.index
    html, newline = read_text(index)
    write_text(index, site.apply_regions(html, regions), newline)
    write_text(r.reviewed, site.reviewed_json(resume, extras, previous, ids))

    paths = r.paths(extras)
    _ok(r.root, "add", "--", *paths)
    if _git(r.root, "diff", "--cached", "--quiet", "--", *paths).returncode == 0:
        return "Nothing to publish: the PDF and the site already match resume.tex."
    what = message.strip() or summary(committed_resume(r), resume)
    _ok(r.root, "commit", "-q", "-m", f"Resume: {what}", "--", *paths)
    sha = _ok(r.root, "rev-parse", "--short", "HEAD").strip()
    if not push:
        return f"Committed {sha} (Resume: {what}); not pushed."
    _ok(r.root, "push", "-q", timeout=PUSH_TIMEOUT_SECONDS)
    log.info("resume published: %s %s", sha, what)
    return f"Published {sha} (Resume: {what}). GitHub Pages has it live in a minute or two."
