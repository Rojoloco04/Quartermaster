"""The portfolio's resume sections, generated from resume.tex plus site.toml.

resume.tex is the source of truth for every resume fact the site shows: titles,
orgs, places, dates, bullets, skills. The site may show *less* (a role's
bullets left out) or *more* (logos, links, project blurbs, tags, activities,
skill levels), and the more lives in the portfolio repo's ``resume/site.toml``,
keyed by the resume entry's id. Site text there can quote the resume with
placeholders (``meta = "Hybrid · {location}"``), so a fact it repeats can't
drift.

What site.toml says in its own words can still go stale when the entry it
describes changes, so each such entry is *reviewed*: ``resume/reviewed.json``
(written by code) holds the hash of the entry as it was when its site text was
last checked, and a changed entry blocks publishing until someone re-checks it
(``qm resume publish --reviewed``, or pressing Confirm on a proposal whose
preview showed that text).

Only the regions between ``<!-- resume:<name> -->`` and ``<!-- /resume:<name> -->``
in index.html are written; everything else on the page is hand-made and kept.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .tex import Entry, Resume, ResumeError, to_html, to_text


SITE_FILE = "site.toml"
REVIEWED_FILE = "reviewed.json"
REGIONS = ("projects", "experience", "education", "skills")

# Extras that put words on the site: an entry with any of these is reviewed
# whenever the resume's version of it changes. (logo, url, thumb are not claims.)
CLAIM_KEYS = ("heading", "org_label", "meta", "dates", "title", "award", "tagline", "blurb", "tags", "activities")

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


@dataclass
class Extras:
    entries: dict[str, dict]
    project_order: list[str]
    skill_labels: dict[str, str]
    skill_levels: dict[str, int]
    pdf: str = "resume.pdf"
    index: str = "index.html"
    raw: dict = field(default_factory=dict)


def load_extras(text: str) -> Extras:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ResumeError(f"site.toml doesn't parse: {exc}") from None
    site = data.get("site", {})
    skills = data.get("skills", {})
    return Extras(
        entries=data.get("entries", {}),
        project_order=list(data.get("projects", {}).get("order", [])),
        skill_labels=dict(skills.get("labels", {})),
        skill_levels={k: int(v) for k, v in skills.get("levels", {}).items()},
        pdf=site.get("pdf", "resume.pdf"),
        index=site.get("index", "index.html"),
        raw=data,
    )


def has_claims(extra: dict) -> bool:
    return any(k in extra for k in CLAIM_KEYS)


# --- Text helpers ----------------------------------------------------------------------


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def site_dates(text: str) -> str:
    """The site writes ranges with an en dash: "Aug. 2022 – May 2026"."""
    return re.sub(r"\s+-\s+", " \u2013 ", text)


def _values(entry: Entry) -> dict[str, str]:
    values = {k: to_text(v) for k, v in entry.fields.items()}
    if "dates" in values:
        values["dates"] = site_dates(values["dates"])
    if entry.kind == "project":
        values["link"] = link_url(entry)
    return values


def fill(template: str, entry: Entry, where: str) -> str:
    """Substitute resume fields into site text; an unknown placeholder is an
    error, since it would otherwise print braces on the live site."""
    values = _values(entry)

    def sub(m: re.Match) -> str:
        if m.group(1) not in values:
            raise ResumeError(f"site.toml entries.{entry.id}.{where}: {{{m.group(1)}}} isn't a field of a "
                              f"\\{entry.kind} (use {', '.join('{' + k + '}' for k in values)})")
        return values[m.group(1)]

    return _PLACEHOLDER.sub(sub, template)


def link_url(entry: Entry) -> str:
    m = re.search(r"\\(?:link|href)\{([^}]*)\}", entry.fields.get("link", ""))
    return m.group(1) if m else ""


def _text(extra: dict, key: str, default: str, entry: Entry) -> str:
    return _esc(fill(extra.get(key, default), entry, key))


def _tags(extra: dict) -> str:
    return " · ".join(_esc(t) for t in extra.get("tags", []))


# --- Blocks ------------------------------------------------------------------------------

_ARROW = '<span class="material-symbols-outlined !text-[12px]">arrow_outward</span>'
_ARTICLE = '<article class="grid sm:grid-cols-[176px_1fr] gap-x-8 gap-y-3 py-7 border-b border-line last:border-b-0">'
_ACTIVITY_LI = '<li class="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-x-6 py-2 border-t border-line">'


def _indent(lines: list[str], spaces: int) -> str:
    return "\n".join((" " * spaces + line) if line else "" for line in lines)


def _org_line(label: str, url: str) -> str:
    if url:
        return (f'<p class="text-sm mt-1"><a href="{_esc(url)}" target="_blank" rel="noopener noreferrer" '
                f'class="lnk">{label}{_ARROW}</a></p>')
    return f'<p class="text-sm mt-1">{label}</p>'


def _logo(extra: dict, alt: str) -> list[str]:
    if not extra.get("logo"):
        return []
    cls = f' class="{_esc(extra["logo_class"])}"' if extra.get("logo_class") else ""
    alt = _esc(extra.get("logo_alt", alt))
    return [f'      <span class="logo mt-0.5"><img src="{_esc(extra["logo"])}" alt="{alt}" loading="lazy"{cls}></span>']


def _article(entry: Entry, extra: dict, heading: str, org_label: str, org_default: str,
             body: list[str], comment: str) -> str:
    lines = [
        f"<!-- {comment} -->",
        _ARTICLE,
        f'  <p class="meta sm:pt-1">{_text(extra, "dates", "{dates}", entry)}</p>',
        '  <div class="min-w-0">',
        '    <div class="flex items-start gap-3">',
        *_logo(extra, to_text(entry.fields[org_default])),
        "      <div>",
        f'        <h3 class="font-medium text-[17px] leading-tight">{heading}</h3>',
        f"        {_org_line(org_label, extra.get('url', ''))}",
        f'        <p class="meta mt-1">{_text(extra, "meta", "{location}", entry)}</p>',
        "      </div>",
        "    </div>",
        *body,
        "  </div>",
        "</article>",
    ]
    return _indent(lines, 12)


def job_block(entry: Entry, extra: dict) -> str:
    body: list[str] = []
    if entry.bullets and extra.get("bullets", True):
        body.append('    <ul class="bullets text-muted text-sm leading-relaxed space-y-2 mt-5">')
        body += [f"      <li>{to_html(b)}</li>" for b in entry.bullets]
        body.append("    </ul>")
    if extra.get("tags"):
        body.append(f'    <p class="meta mt-5">{_tags(extra)}</p>')
    return _article(entry, extra, _text(extra, "heading", "{role}", entry),
                    _text(extra, "org_label", "{org}", entry), "org", body, to_text(entry.fields["org"]))


def school_block(entry: Entry, extra: dict, resume: Resume) -> str:
    body: list[str] = []
    activities = extra.get("activities", [])
    if activities:
        body += ["", "    <!-- Activities -->", '    <p class="label mt-7 mb-3">Activities</p>', '    <ul class="text-sm">']
        for item in activities:
            text, dates = activity(item, resume, entry.id)
            body += [f"      {_ACTIVITY_LI}", f"        <span>{text}</span>",
                     f'        <span class="meta shrink-0">{dates}</span>', "      </li>"]
        body.append("    </ul>")
    return _article(entry, extra, _text(extra, "heading", "{degree}", entry),
                    _text(extra, "org_label", "{school}", entry), "school", body,
                    extra.get("logo_alt", to_text(entry.fields["school"])))


def activity(item: dict, resume: Resume, owner: str) -> tuple[str, str]:
    """An activities line: a resume role by id (its title, org and dates, as
    the resume says them) or site-only text."""
    if "role" in item:
        role = resume.by_id().get(item["role"])
        if role is None or role.kind != "role":
            raise ResumeError(f"site.toml entries.{owner}.activities: no \\role{{{item['role']}}} in resume.tex")
        return (_esc(f"{role.text('title')} · {role.text('org')}"), _esc(site_dates(role.text("dates"))))
    if "text" not in item:
        raise ResumeError(f"site.toml entries.{owner}.activities: each needs role = \"<id>\" or text = \"...\"")
    return _esc(item["text"]), _esc(item.get("dates", ""))


def project_block(pid: str, entry: Entry | None, extra: dict) -> str:
    if entry is not None:
        title = _text(extra, "title", "{name}", entry)
        url = extra.get("url") or link_url(entry)
        award = _text(extra, "award", "{award}", entry) if (extra.get("award") or entry.fields.get("award")) else ""
        tagline = _text(extra, "tagline", "", entry)
        blurb = _text(extra, "blurb", "{summary}", entry)
    else:
        title, url = _esc(extra.get("title", pid)), extra.get("url", "")
        award, tagline, blurb = _esc(extra.get("award", "")), _esc(extra.get("tagline", "")), _esc(extra.get("blurb", ""))
    label = _esc(extra.get("link_label", "GitHub"))
    head = (f'<a href="{_esc(url)}" target="_blank" rel="noopener noreferrer"' if url else "<div")
    lines = [
        f"<!-- {title} -->",
        head,
        f'   class="project group grid sm:grid-cols-[180px_1fr] md:grid-cols-[200px_1fr] gap-5 md:gap-8 py-7 '
        f'border-b border-line last:border-b-0"' + (f' aria-label="View {title} on {label}">' if url else ">"),
    ]
    if extra.get("thumb"):
        lines.append(f'  <div class="thumb"><img src="{_esc(extra["thumb"])}" alt="" loading="lazy" /></div>')
    lines += [
        '  <div class="min-w-0">',
        '    <div class="flex items-baseline justify-between gap-4">',
        f'      <h3 class="project-title font-medium text-[17px]">{title}</h3>',
    ]
    if url:
        lines.append(f'      <span class="meta shrink-0">{label} {_ARROW}</span>')
    lines.append("    </div>")
    if award:
        lines.append(f'    <p class="meta mt-1 !text-primary">{award}</p>')
    elif tagline:
        lines.append(f'    <p class="meta mt-1">{tagline}</p>')
    if blurb:
        lines += ['    <p class="text-muted text-sm leading-relaxed mt-3">', f"      {blurb}", "    </p>"]
    if extra.get("tags"):
        lines.append(f'    <p class="meta mt-3">{_tags(extra)}</p>')
    lines += ["  </div>", "</a>" if url else "</div>"]
    return _indent(lines, 12)


def skills_block(label: str, items: list[tuple[str, int]]) -> str:
    lines = ["<div>", f'  <p class="label pb-2 border-b border-line">{_esc(label)}</p>', "  <ul>"]
    lines += [f'    <li class="skill-row" data-level="{level}">{_esc(name)}</li>' for name, level in items]
    lines += ["  </ul>", "</div>"]
    return _indent(lines, 12)


# --- Regions -----------------------------------------------------------------------------


def render(resume: Resume, extras: Extras) -> dict[str, str]:
    """Each region's generated blocks. Raises on anything the site couldn't
    show truthfully (an unknown id, a resume project or role with no place on
    the site, a skill with no level)."""
    problems = structure_problems(resume, extras)
    if problems:
        raise ResumeError("; ".join(problems))
    by_id = resume.by_id()
    ex = extras.entries
    regions = {
        "experience": [job_block(e, ex.get(e.id, {})) for e in resume.of_kind("job") if not ex.get(e.id, {}).get("hidden")],
        "education": [school_block(e, ex.get(e.id, {}), resume) for e in resume.of_kind("school")
                      if not ex.get(e.id, {}).get("hidden")],
        "projects": [project_block(pid, by_id.get(pid), ex.get(pid, {})) for pid in extras.project_order],
    }
    skills = []
    for group in resume.skills:
        category = to_text(group.category)
        skills.append(skills_block(extras.skill_labels.get(category, category),
                                   [(name, extras.skill_levels[name]) for name, _ in group.items]))
    regions["skills"] = skills
    return {name: "\n\n".join(blocks) for name, blocks in regions.items()}


def structure_problems(resume: Resume, extras: Extras) -> list[str]:
    problems: list[str] = []
    by_id = resume.by_id()
    for eid, extra in extras.entries.items():
        if eid not in by_id and not extra.get("site_only"):
            problems.append(f"site.toml has entries.{eid} but resume.tex has no entry {eid!r} "
                            "(remove it, or mark it site_only = true)")
        if eid in by_id and extra.get("site_only"):
            problems.append(f"entries.{eid} is site_only but resume.tex has it")
    for pid in extras.project_order:
        if pid not in by_id and not extras.entries.get(pid, {}).get("site_only"):
            problems.append(f"projects.order lists {pid!r}, which is neither in resume.tex nor a site_only entry")
    for e in resume.of_kind("project"):
        if e.id not in extras.project_order and not extras.entries.get(e.id, {}).get("hidden"):
            problems.append(f"resume project {e.id!r} isn't on the site: add it to projects.order in site.toml "
                            "(or set hidden = true)")
    shown_roles = {item.get("role") for extra in extras.entries.values() for item in extra.get("activities", [])}
    for e in resume.of_kind("role"):
        if e.id not in shown_roles and not extras.entries.get(e.id, {}).get("hidden"):
            problems.append(f"resume role {e.id!r} isn't on the site: add {{ role = \"{e.id}\" }} to a school's "
                            "activities in site.toml (or set hidden = true)")
    for group in resume.skills:
        for name, _ in group.items:
            if name not in extras.skill_levels:
                problems.append(f"skill {name!r} has no level in site.toml [skills.levels] (1-4)")
    return problems


def stale(resume: Resume, extras: Extras, reviewed: dict[str, str]) -> list[Entry]:
    """Entries whose site text was last reviewed against a different version
    of what the resume says about them."""
    return [e for e in resume.entries
            if has_claims(extras.entries.get(e.id, {})) and reviewed.get(e.id) != e.digest()]


def site_text(entry: Entry, extras: Extras) -> list[str]:
    """What the site says about an entry in site.toml's own words, for a
    reviewer: placeholders filled, one line per field."""
    extra = extras.entries.get(entry.id, {})
    lines = []
    for key in CLAIM_KEYS:
        if key not in extra:
            continue
        value = extra[key]
        if key == "tags":
            lines.append(f"tags: {' · '.join(value)}")
        elif key == "activities":
            continue
        else:
            lines.append(f"{key}: {fill(value, entry, key)}")
    return lines


def load_reviewed(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def reviewed_json(resume: Resume, extras: Extras, previous: dict[str, str], ids: set[str]) -> str:
    """reviewed.json with ``ids`` marked reviewed at their current version,
    and entries that no longer have site text dropped."""
    current = {e.id: e.digest() for e in resume.entries if has_claims(extras.entries.get(e.id, {}))}
    out = {eid: (current[eid] if eid in ids else previous.get(eid, "")) for eid in current}
    return json.dumps({k: v for k, v in out.items() if v}, indent=2, sort_keys=True) + "\n"


_MARKER = r"<!-- resume:{name}\b[^>]*-->"


def _region_span(html: str, name: str) -> tuple[int, int, int]:
    start = re.search(_MARKER.format(name=name), html)
    end = re.search(rf"\n([ \t]*)<!-- /resume:{name} -->", html)
    if not start or not end or end.start() < start.end():
        raise ResumeError(f"index.html has no <!-- resume:{name} --> ... <!-- /resume:{name} --> markers")
    return start.end(), end.start(), len(end.group(1))


def current_region(html: str, name: str) -> str:
    begin, end, _ = _region_span(html, name)
    return html[begin:end].strip("\n")


def apply_regions(html: str, regions: dict[str, str]) -> str:
    for name in REGIONS:
        begin, end, _ = _region_span(html, name)
        html = html[:begin] + "\n\n" + regions[name] + "\n" + html[end:]
    return html
