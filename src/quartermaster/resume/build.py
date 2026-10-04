"""Turning resume.tex into the PDF, and proving the PDF says what the source says.

Tectonic does the typesetting: one executable that fetches the TeX packages it
needs on first use, so there is no TeX distribution to install or keep current.
``qm resume setup`` downloads a pinned release (sha256 checked) into the
per-user data dir, outside both repos.

A build passes only if the PDF is one page and its extracted text contains
every bullet, in order, word for word. That is the "machine-readable" promise:
what an applicant-tracking system pulls out of the file is what was written.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .tex import Resume, ResumeError, to_text


log = logging.getLogger(__name__)

TECTONIC_VERSION = "0.17.0"
TECTONIC_URL = (
    "https://github.com/tectonic-typesetting/tectonic/releases/download/"
    f"tectonic%40{TECTONIC_VERSION}/tectonic-{TECTONIC_VERSION}-x86_64-pc-windows-msvc.zip"
)
TECTONIC_SHA256 = "f61ce51f0b0ade1015b7de7ef368541c5424e9756ecbd0d7af97d6d48030845f"
# The first build downloads the TeX bundle (a few hundred MB, cached after).
BUILD_TIMEOUT_SECONDS = 600
MAX_PAGES = 1
# Every build stamps this date instead of now, so the same source always gives
# the same bytes: an unchanged resume never shows up as a changed PDF in git.
SOURCE_DATE_EPOCH = "1767225600"  # 2026-01-01


def tectonic_path() -> Path:
    from platformdirs import user_data_path

    return user_data_path("quartermaster", appauthor=False) / "tectonic" / "tectonic.exe"


def setup() -> str:
    exe = tectonic_path()
    if exe.exists():
        return f"Tectonic is already installed: {exe}"
    data = httpx.get(TECTONIC_URL, timeout=120, follow_redirects=True).raise_for_status().content
    digest = hashlib.sha256(data).hexdigest()
    if digest != TECTONIC_SHA256:
        raise ResumeError(f"the Tectonic download's sha256 is {digest}, expected {TECTONIC_SHA256}; not installed")
    exe.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        member = next((n for n in zf.namelist() if n.endswith("tectonic.exe")), None)
        if member is None:
            raise ResumeError("the Tectonic download has no tectonic.exe")
        exe.write_bytes(zf.read(member))
    return f"Installed Tectonic {TECTONIC_VERSION}: {exe}. The first build downloads its TeX bundle (once)."


@dataclass
class Built:
    pdf: bytes
    pages: int
    text: str


def compile_pdf(tex_dir: Path, tex_name: str = "resume.tex", source: str | None = None) -> bytes:
    """Typeset ``tex_dir/tex_name`` (or ``source`` in its place, for a
    proposal not yet written) in a scratch copy; the repo is never written."""
    exe = tectonic_path()
    if not exe.exists():
        raise ResumeError("Tectonic isn't installed. The owner runs: qm resume setup")
    with tempfile.TemporaryDirectory(prefix="qm-resume-") as tmp:
        work = Path(tmp)
        for path in tex_dir.iterdir():
            if path.is_file() and path.suffix in (".tex", ".cls", ".sty"):
                shutil.copy2(path, work / path.name)
        if source is not None:
            (work / tex_name).write_text(source, encoding="utf-8")
        result = subprocess.run(
            [str(exe), "-X", "compile", "--keep-logs", tex_name],
            cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH},
            timeout=BUILD_TIMEOUT_SECONDS,
        )
        pdf = work / Path(tex_name).with_suffix(".pdf").name
        if result.returncode != 0 or not pdf.exists():
            # Fontconfig's complaint is printed on every run (Tectonic finds fonts without it).
            errors = [ln.strip() for ln in (result.stderr + result.stdout).splitlines()
                      if (ln.startswith("!") or "error" in ln.lower()) and "Fontconfig" not in ln]
            raise ResumeError("LaTeX build failed: " + ("; ".join(errors[:5]) or result.stderr.strip()[-500:]))
        return pdf.read_bytes()


def pdf_text(pdf: bytes) -> tuple[int, str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    return len(reader.pages), "\n".join(page.extract_text() or "" for page in reader.pages)


def _squash(text: str) -> str:
    """For comparing what was written with what the PDF yields: same letters,
    whatever happened to spacing, quotes, ligatures and bullets."""
    # Before NFKC, which would turn a small tilde into a space and a combining mark.
    text = text.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "˜": "~"}))
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", "", text)


def missing_text(resume: Resume, text: str) -> list[str]:
    """Bullets (and headings) not found in the PDF's text, in order. Empty
    means a reader gets out exactly what was written."""
    haystack = _squash(text)
    missing: list[str] = []
    pos = 0
    wanted = [resume.name]
    for entry in resume.entries:
        wanted += [entry.text(k) for k in entry.fields if k != "link" and entry.text(k)]
        wanted += entry.bullet_texts()
    for needle in wanted:
        squashed = _squash(needle)
        found = haystack.find(squashed, pos)
        if found == -1:
            missing.append(needle)
        else:
            pos = found
    skills = [f"{to_text(g.category)}:" for g in resume.skills]
    missing += [s for s in skills if _squash(s) not in haystack]
    return missing


def build(resume: Resume, tex_dir: Path, source: str | None = None) -> Built:
    pdf = compile_pdf(tex_dir, source=source)
    pages, text = pdf_text(pdf)
    if pages > MAX_PAGES:
        raise ResumeError(f"the resume is {pages} pages; it has to fit on {MAX_PAGES}. Cut something.")
    missing = missing_text(resume, text)
    if missing:
        raise ResumeError("the PDF's text doesn't match the source (an ATS would read it wrong): "
                          + "; ".join(repr(m[:60]) for m in missing[:3]))
    return Built(pdf=pdf, pages=pages, text=text)
