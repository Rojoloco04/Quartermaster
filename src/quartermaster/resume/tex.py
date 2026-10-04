"""resume.tex, read in code.

The resume's LaTeX is the source of truth for everything it says, so it is
written in a small fixed vocabulary (defined in the portfolio repo's
``resume/resume.cls``) that this module can read without guessing:

    \\header{name}{contact items, separated by \\sep}
    \\section{Experience}
    \\school{id}{school}{location}{dates}{degree}{note}
    \\job{id}{org}{location}{role}{dates}
    \\project{id}{name}{award}{link}{dates}{summary}
    \\role{id}{org}{title}{dates}
    \\skills{category}{item, item (note), ...}
    \\begin{bullets} \\item ... \\end{bullets}    (after an entry: its bullets)

Anything else in the document body is an error naming its line, never
skipped: a silently dropped bullet is exactly the desync this exists to
prevent. Inline text allows a few commands (escapes, bold/italic, links,
superscripts, ``$\\sim$``); an unknown one is an error too.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field


class ResumeError(RuntimeError):
    """Something the owner should read: a parse error, a failed build, a git refusal."""


# Each entry macro's arguments, in order. The first is always the entry's id.
ENTRY_ARGS: dict[str, tuple[str, ...]] = {
    "school": ("id", "school", "location", "dates", "degree", "note"),
    "job": ("id", "org", "location", "role", "dates"),
    "project": ("id", "name", "award", "link", "dates", "summary"),
    "role": ("id", "org", "title", "dates"),
}

_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass
class Entry:
    kind: str  # school | job | project | role
    id: str
    section: str
    fields: dict[str, str]  # raw TeX, by ENTRY_ARGS name (id excluded)
    bullets: list[str] = field(default_factory=list)  # raw TeX
    line: int = 0

    def text(self, name: str) -> str:
        return to_text(self.fields.get(name, ""))

    def bullet_texts(self) -> list[str]:
        return [to_text(b) for b in self.bullets]

    def digest(self) -> str:
        """A short hash of everything the resume says about this entry: what
        the site's extras for it were last reviewed against."""
        blob = json.dumps({"kind": self.kind, "fields": self.fields, "bullets": self.bullets}, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


@dataclass
class SkillGroup:
    category: str  # raw TeX
    items: list[tuple[str, str]]  # (name, note) as plain text: "Spanish (Advanced)" -> ("Spanish", "Advanced")


@dataclass
class Resume:
    name: str = ""
    contact: list[str] = field(default_factory=list)  # raw TeX, one per \sep-separated item
    entries: list[Entry] = field(default_factory=list)
    skills: list[SkillGroup] = field(default_factory=list)

    def by_id(self) -> dict[str, Entry]:
        return {e.id: e for e in self.entries}

    def of_kind(self, kind: str) -> list[Entry]:
        return [e for e in self.entries if e.kind == kind]


# --- Reading the document ----------------------------------------------------------


class _Reader:
    def __init__(self, src: str, offset_line: int):
        self.src = src
        self.pos = 0
        self.offset_line = offset_line

    def line(self, pos: int | None = None) -> int:
        return self.offset_line + self.src.count("\n", 0, self.pos if pos is None else pos)

    def fail(self, message: str, pos: int | None = None) -> ResumeError:
        return ResumeError(f"resume.tex line {self.line(pos)}: {message}")

    def skip_space(self) -> None:
        while self.pos < len(self.src):
            ch = self.src[self.pos]
            if ch.isspace():
                self.pos += 1
            elif ch == "%":
                end = self.src.find("\n", self.pos)
                self.pos = len(self.src) if end == -1 else end + 1
            else:
                return

    def done(self) -> bool:
        self.skip_space()
        return self.pos >= len(self.src)

    def command(self) -> str:
        if self.src[self.pos] != "\\":
            snippet = self.src[self.pos : self.pos + 40].split("\n", 1)[0]
            raise self.fail(f"text outside any entry: {snippet!r}")
        m = re.compile(r"\\([A-Za-z]+)").match(self.src, self.pos)
        if not m:
            raise self.fail(f"unexpected {self.src[self.pos:self.pos + 2]!r}")
        self.pos = m.end()
        return m.group(1)

    def group(self, what: str) -> str:
        self.skip_space()
        if self.pos >= len(self.src) or self.src[self.pos] != "{":
            raise self.fail(f"expected {{...}} for {what}")
        start = self.pos + 1
        depth = 0
        i = self.pos
        while i < len(self.src):
            ch = self.src[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "%":  # a comment inside an argument runs to the end of its line
                end = self.src.find("\n", i)
                i = len(self.src) if end == -1 else end + 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    self.pos = i + 1
                    return _strip_comments(self.src[start:i]).strip()
            i += 1
        raise self.fail(f"unclosed {{ in {what}", start)


def _strip_comments(text: str) -> str:
    """Drop % comments (not \\%) and join the lines they broke."""
    out = re.sub(r"(?<!\\)%[^\n]*\n?[ \t]*", "", text)
    return re.sub(r"\s+", " ", out)


def _split_items(raw: str) -> list[str]:
    """Split a bullets body on \\item, ignoring \\item inside braces."""
    parts, depth, start, i = [], 0, None, 0
    while i < len(raw):
        if raw.startswith("\\item", i) and depth == 0 and not raw[i + 5 : i + 6].isalpha():
            if start is not None:
                parts.append(raw[start:i])
            start = i + 5
            i += 5
            continue
        ch = raw[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif start is None and not ch.isspace():
            raise ResumeError(f"text before the first \\item in a bullets list: {raw[i:i + 40]!r}")
        i += 1
    if start is not None:
        parts.append(raw[start:])
    return [_strip_comments(p).strip() for p in parts]


def _split_skills(raw: str) -> list[tuple[str, str]]:
    items = []
    for part in re.split(r",\s*(?![^{]*\})", raw):
        text = to_text(part).strip()
        if not text:
            continue
        m = re.match(r"^(.*?)\s*\(([^()]*)\)$", text)
        items.append((m.group(1), m.group(2)) if m else (text, ""))
    return items


def parse(source: str) -> Resume:
    begin = source.find("\\begin{document}")
    end = source.find("\\end{document}")
    if begin == -1 or end == -1:
        raise ResumeError("resume.tex has no \\begin{document} ... \\end{document}")
    body_start = begin + len("\\begin{document}")
    reader = _Reader(source[body_start:end], offset_line=source.count("\n", 0, body_start) + 1)
    resume = Resume()
    section = ""
    seen: set[str] = set()

    while not reader.done():
        at = reader.pos
        name = reader.command()
        if name == "header":
            resume.name = to_text(reader.group("the name"))
            resume.contact = [c.strip() for c in reader.group("the contact line").split("\\sep")]
        elif name == "section":
            section = to_text(reader.group("the section title"))
        elif name in ENTRY_ARGS:
            values = [reader.group(f"\\{name}'s {arg}") for arg in ENTRY_ARGS[name]]
            entry_id = values[0]
            if not _ID.match(entry_id):
                raise reader.fail(f"\\{name} id {entry_id!r}: use lowercase letters, digits and dashes", at)
            if entry_id in seen:
                raise reader.fail(f"duplicate id {entry_id!r}", at)
            if not section:
                raise reader.fail(f"\\{name}{{{entry_id}}} comes before any \\section", at)
            seen.add(entry_id)
            fields = dict(zip(ENTRY_ARGS[name][1:], values[1:]))
            for key, value in fields.items():
                try:
                    to_text(value)
                except ResumeError as exc:
                    raise reader.fail(f"{entry_id} {key}: {exc}", at) from None
            resume.entries.append(Entry(name, entry_id, section, fields, line=reader.line(at)))
        elif name == "skills":
            category = reader.group("the skills category")
            try:
                resume.skills.append(SkillGroup(category, _split_skills(reader.group("the skills"))))
            except ResumeError as exc:
                raise reader.fail(str(exc), at) from None
        elif name == "begin":
            env = reader.group("the environment")
            if env != "bullets":
                raise reader.fail(f"only \\begin{{bullets}} is allowed in the body, not {env!r}", at)
            close = reader.src.find("\\end{bullets}", reader.pos)
            if close == -1:
                raise reader.fail("\\begin{bullets} is never closed", at)
            if not resume.entries:
                raise reader.fail("a bullets list before any entry", at)
            entry = resume.entries[-1]
            if entry.bullets:
                raise reader.fail(f"{entry.id} already has a bullets list", at)
            try:
                entry.bullets = _split_items(reader.src[reader.pos:close])
                for bullet in entry.bullets:
                    to_text(bullet)
            except ResumeError as exc:
                raise reader.fail(f"{entry.id}: {exc}", at) from None
            reader.pos = close + len("\\end{bullets}")
        else:
            raise reader.fail(f"\\{name} isn't part of the resume's vocabulary", at)

    if not resume.name:
        raise ResumeError("resume.tex has no \\header")
    return resume


# --- Inline text ---------------------------------------------------------------------

_ESCAPES = {"&": "&", "%": "%", "$": "$", "#": "#", "_": "_", "{": "{", "}": "}"}
_WRAP = {"textbf": "strong", "textit": "em", "emph": "em", "textsuperscript": "sup", "underline": ""}


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _convert(src: str, html: bool) -> str:
    out: list[str] = []
    i = 0

    def emit(text: str) -> None:
        out.append(_html_escape(text) if html else text)

    def arg(at: int) -> tuple[str, int]:
        while at < len(src) and src[at] == " ":
            at += 1
        if at >= len(src) or src[at] != "{":
            raise ResumeError(f"expected {{...}} after a command in {src!r}")
        depth, j = 0, at
        while j < len(src):
            if src[j] == "\\":
                j += 2
                continue
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    return src[at + 1 : j], j + 1
            j += 1
        raise ResumeError(f"unclosed {{ in {src!r}")

    while i < len(src):
        ch = src[i]
        if ch == "\\":
            nxt = src[i + 1 : i + 2]
            if nxt in _ESCAPES:
                emit(_ESCAPES[nxt])
                i += 2
                continue
            if nxt == ",":  # thin space
                emit(" ")
                i += 2
                continue
            m = re.compile(r"[A-Za-z]+").match(src, i + 1)
            if not m:
                raise ResumeError(f"unexpected {src[i:i + 2]!r} in {src!r}")
            name, i = m.group(0), m.end()
            if name in _WRAP:
                inner, i = arg(i)
                tag = _WRAP[name]
                body = _convert(inner, html)
                out.append(f"<{tag}>{body}</{tag}>" if html and tag else body)
            elif name in ("href", "link"):
                url, i = arg(i)
                label, i = arg(i)
                body = _convert(label, html)
                out.append(f'<a href="{_html_escape(url)}">{body}</a>' if html else body)
            elif name == "textasciitilde":
                if src.startswith("{}", i):
                    i += 2
                emit("~")
            elif name == "sep":
                emit(" | ")
            else:
                raise ResumeError(f"\\{name} isn't allowed in resume text")
            continue
        if src.startswith("$\\sim$", i):
            emit("~")
            i += len("$\\sim$")
            continue
        if ch == "$":
            raise ResumeError(f"math mode isn't allowed in resume text (only $\\sim$): {src!r}")
        if src.startswith("---", i):
            emit("—")
            i += 3
            continue
        if src.startswith("--", i):
            emit("–")
            i += 2
            continue
        if src.startswith("``", i):
            emit("“")
            i += 2
            continue
        if src.startswith("''", i):
            emit("”")
            i += 2
            continue
        if ch in "{}":
            i += 1
            continue
        if ch == "~":
            emit(" ")
            i += 1
            continue
        emit(ch)
        i += 1
    return re.sub(r"\s+", " ", "".join(out)).strip()


def to_text(tex: str) -> str:
    """Plain text: what a reader (or an ATS) gets out of it."""
    return _convert(tex, html=False)


def to_html(tex: str) -> str:
    """Escaped HTML for the site, with bold/italic/links/superscripts kept."""
    return _convert(tex, html=True)


# --- What changed --------------------------------------------------------------------


@dataclass
class Change:
    id: str
    kind: str  # added | removed | changed
    lines: list[str]  # "- old" / "+ new" lines, plain text


def diff(old: Resume, new: Resume) -> list[Change]:
    """Entry by entry, what a change to resume.tex does, in plain text: what a
    person confirming it needs to read."""
    import difflib

    before, after = old.by_id(), new.by_id()
    changes: list[Change] = []
    for eid, entry in after.items():
        prev = before.get(eid)
        if prev is None:
            lines = [f"+ {k}: {entry.text(k)}" for k in entry.fields if entry.text(k)]
            changes.append(Change(eid, "added", lines + [f"+ • {b}" for b in entry.bullet_texts()]))
            continue
        if prev.digest() == entry.digest():
            continue
        lines = []
        for key in entry.fields:
            if prev.text(key) != entry.text(key):
                lines += [f"- {key}: {prev.text(key)}", f"+ {key}: {entry.text(key)}"]
        a, b = prev.bullet_texts(), entry.bullet_texts()
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
            if op != "equal":
                lines += [f"- • {t}" for t in a[i1:i2]] + [f"+ • {t}" for t in b[j1:j2]]
        if not lines:  # same text, different markup
            lines = ["~ formatting only"]
        changes.append(Change(eid, "changed", lines))
    for eid in before:
        if eid not in after:
            changes.append(Change(eid, "removed", [f"- {before[eid].text(next(iter(before[eid].fields)))}"]))
    old_skills = {to_text(g.category): [n for n, _ in g.items] for g in old.skills}
    new_skills = {to_text(g.category): [n for n, _ in g.items] for g in new.skills}
    if old_skills != new_skills:
        lines = []
        for cat in dict.fromkeys([*old_skills, *new_skills]):
            if old_skills.get(cat) != new_skills.get(cat):
                if cat in old_skills:
                    lines.append(f"- {cat}: {', '.join(old_skills[cat])}")
                if cat in new_skills:
                    lines.append(f"+ {cat}: {', '.join(new_skills[cat])}")
        changes.append(Change("skills", "changed", lines))
    return changes
