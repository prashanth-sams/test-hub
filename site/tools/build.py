#!/usr/bin/env python3
"""Build the Test Hub documentation website.

Each page in ``content/`` is a body fragment with a small JSON front-matter
block. This script wraps it in the shared shell — head, top bar, sidebar, table
of contents, prev/next and footer — and writes a complete, standalone HTML file
into ``docs/``. It also writes the landing page and the client-side search
index.

The output has no runtime dependency on this script, on a server, or on any
network origin: the built pages open the same way from ``file://`` and from
GitHub Pages. That is the same constraint the product itself is under (ADR-0001
D7), and it is why there is no bundler here and no CDN in the markup.

Standard library only, on purpose. A docs build that needs ``pip install``
is a docs build that rots the first time somebody clones the repository on a
fresh machine.

    python3 tools/build.py            # build everything
    python3 tools/build.py --check    # fail if the committed output is stale
"""

from __future__ import annotations

import json
import re
import sys
from html import escape, unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
OUT = ROOT / "docs"

SITE_TITLE = "Test Hub"
TAGLINE = "Universal test observability — every team, framework and test run in one place."
REPO = "https://github.com/prashanth-sams/test-hub"
PYPI = "https://pypi.org/project/test-hub/"

# --------------------------------------------------------------- navigation

# (group label, [(slug, title, icon key)])
SIDEBAR = [
    (
        "Start here",
        [
            ("getting-started", "Getting started", "rocket"),
            ("features", "Features", "sparkle"),
            ("dashboard-tour", "Dashboard tour", "layout"),
        ],
    ),
    (
        "Reference",
        [
            ("cli-reference", "CLI reference", "terminal"),
            ("configuration", "Configuration", "sliders"),
            ("rest-api", "REST API", "server"),
            ("python-sdk", "Python SDK", "code"),
        ],
    ),
    (
        "Contracts",
        [
            ("adapters", "Adapters", "plug"),
            ("canonical-schema", "Canonical schema", "package"),
            ("events", "Event contract", "stream"),
            ("metrics", "Metrics", "chart"),
        ],
    ),
    (
        "Operating it",
        [
            ("security", "Security", "shield"),
            ("deployment", "Deployment", "box"),
            ("ci-integrations", "CI integrations", "pipeline"),
        ],
    ),
    (
        "Project",
        [
            ("faq", "FAQ", "help"),
            ("contributing", "Contributing", "heart"),
        ],
    ),
]

#: Flat order drives prev/next and the order pages are built in.
ORDER = [slug for _, items in SIDEBAR for slug, _, _ in items]
TITLES = {slug: title for _, items in SIDEBAR for slug, title, _ in items}

# Stroke icons, drawn on a 24x24 grid. Inline rather than a sprite sheet or a
# font: an icon font is a network request and a sprite needs a <use> that
# does not resolve over file://.
ICONS = {
    "rocket": '<path d="M4.5 16.5c-1.5 1.3-2 5-2 5s3.7-.5 5-2c.7-.8.7-2.1-.1-2.9a2 2 0 0 0-2.9 0z"/><path d="M12 15 9 12a11 11 0 0 1 2-6c2.2-3 5.5-3.5 8-3.5 0 2.5-.5 5.8-3.5 8a11 11 0 0 1-6 2z"/><path d="M9 12H5s.4-2.5 2-3.5c1.7-1 3.5-.5 3.5-.5M12 15v4s2.5-.4 3.5-2c1-1.7.5-3.5.5-3.5"/>',
    "sparkle": '<path d="m12 3 2.2 5.8L20 11l-5.8 2.2L12 19l-2.2-5.8L4 11l5.8-2.2z"/><path d="M19 3v4M17 5h4M5 17v2M4 18h2"/>',
    "layout": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/>',
    "terminal": '<rect x="2" y="3" width="20" height="18" rx="2"/><path d="m6 9 3 3-3 3M13 15h5"/>',
    "sliders": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    "server": '<rect x="2" y="3" width="20" height="7" rx="2"/><rect x="2" y="14" width="20" height="7" rx="2"/><path d="M6 6.5h.01M6 17.5h.01"/>',
    "code": '<path d="m8 6-6 6 6 6M16 6l6 6-6 6"/>',
    "plug": '<path d="M9 2v6M15 2v6M7 8h10v3a5 5 0 0 1-5 5 5 5 0 0 1-5-5z"/><path d="M12 16v6"/>',
    "package": '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="m3.3 7 8.7 5 8.7-5M12 22V12"/>',
    "stream": '<path d="M3 5c6 0 6 14 12 14M3 12c4 0 4 7 8 7M3 19h1"/><circle cx="19" cy="5" r="2"/><circle cx="19" cy="12" r="2"/>',
    "chart": '<path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
    "box": '<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/>',
    "pipeline": '<circle cx="5" cy="6" r="2.5"/><circle cx="5" cy="18" r="2.5"/><circle cx="19" cy="12" r="2.5"/><path d="M5 8.5v7M7.5 6h5a4 4 0 0 1 4 4v.5M7.5 18h5a4 4 0 0 0 4-4v-.5"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.2 9.2a2.9 2.9 0 0 1 5.6 1c0 2-2.8 2.8-2.8 2.8M12 17h.01"/>',
    "heart": '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1.1 1L12 21l7.7-7.6 1.1-1a5.5 5.5 0 0 0 0-7.8z"/>',
}

# The brand mark: four bars, the pass/fail/skip/flaky language of the product,
# in the dashboard's own accent. Currentcolor is deliberately not used — the
# mark should read the same on both themes.
BRAND_MARK = (
    '<svg class="brand__mark" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
    '<rect x="2.5" y="12.5" width="4" height="8.5" rx="1.2" fill="var(--status-passed)"/>'
    '<rect x="8.2" y="6.5" width="4" height="14.5" rx="1.2" fill="var(--accent)"/>'
    '<rect x="13.9" y="9.5" width="4" height="11.5" rx="1.2" fill="var(--status-skipped)"/>'
    '<rect x="19.6" y="3" width="2.4" height="18" rx="1.2" fill="var(--status-failed)"/>'
    "</svg>"
)

GITHUB_GLYPH = (
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false"><path '
    'd="M12 .5A11.5 11.5 0 0 0 .5 12a11.5 11.5 0 0 0 7.86 10.92c.58.1.79-.25.79-.56v-2c-3.2.7'
    "-3.88-1.37-3.88-1.37-.53-1.34-1.29-1.7-1.29-1.7-1.05-.72.08-.7.08-.7 1.16.08 1.77 1.2 1.77 "
    "1.2 1.03 1.77 2.71 1.26 3.37.96.1-.75.4-1.26.73-1.55-2.56-.29-5.25-1.28-5.25-5.7 0-1.26.45"
    "-2.29 1.19-3.1-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.18 1.18a11 11 0 0 1 5.8 0c2.2-1.5 "
    "3.17-1.18 3.17-1.18.63 1.59.24 2.76.12 3.05.74.81 1.18 1.84 1.18 3.1 0 4.43-2.69 5.4-5.26 "
    "5.69.41.36.78 1.06.78 2.14v3.17c0 .31.21.67.8.56A11.5 11.5 0 0 0 23.5 12 11.5 11.5 0 0 0 "
    '12 .5z"/></svg>'
)

SEARCH_GLYPH = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" aria-hidden="true" focusable="false"><circle cx="11" cy="11" r="7"/>'
    '<path d="m20 20-3.6-3.6"/></svg>'
)

TOP_NAV = [
    ("getting-started", "Docs"),
    ("dashboard-tour", "Dashboard"),
    ("rest-api", "API"),
    ("cli-reference", "CLI"),
    ("deployment", "Deploy"),
]


def searchbtn(prefix: str) -> str:
    """The top-bar search trigger.

    Everything else about search is built by ``site.js`` from the generated
    index. ``data-search-root`` is how that script turns the site-relative paths
    in ``search-index.json`` into links that work from both ``/`` and ``/docs/``.
    """
    return (
        f'<button class="searchbtn" type="button" data-search-root="{prefix}" '
        'aria-label="Search the documentation" aria-haspopup="dialog">'
        f"{SEARCH_GLYPH}"
        '<span class="searchbtn__label">Search</span>'
        '<kbd class="searchbtn__kbd"><span class="searchbtn__mod">Ctrl</span>K</kbd>'
        "</button>"
    )


def head(meta: dict, prefix: str, *, slug: str) -> str:
    """The document head, including the pre-paint theme bootstrap.

    The theme is resolved and written onto ``<html>`` before the first paint, so
    a reader on a dark desktop never sees a white flash. Every storage access is
    wrapped: a browser with site data blocked throws on `localStorage`, and a
    docs page that fails to render because of a preference is a bad trade.
    """
    title = unescape(meta["title"])
    desc = meta.get("description", "")
    full = f"{title} · {SITE_TITLE}" if title != SITE_TITLE else f"{SITE_TITLE} — documentation"
    canonical_slug = "" if slug == "__index__" else f"docs/{slug}.html"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(full)}</title>
<meta name="description" content="{escape(desc)}">
<meta name="color-scheme" content="light dark">
<meta name="generator" content="test-hub site/tools/build.py">
<meta property="og:title" content="{escape(full)}">
<meta property="og:description" content="{escape(desc)}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="{SITE_TITLE}">
<link rel="icon" href="{prefix}assets/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="{prefix}assets/css/site.css">
<script>
/* Resolve the theme before first paint: no white flash for a dark reader.
   An explicit choice wins; otherwise follow the OS. Wrapped because reading
   localStorage throws outright in a browser configured to block site data. */
(function () {{
  var theme = null;
  try {{ theme = localStorage.getItem('testHubDocsTheme'); }} catch (e) {{ theme = null; }}
  if (theme !== 'light' && theme !== 'dark') {{
    theme = window.matchMedia
      && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }}
  document.documentElement.setAttribute('data-theme', theme);
  document.documentElement.classList.add('js');
}}());
</script>
<!-- canonical: {canonical_slug or "index.html"} -->
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
"""


def topbar(slug: str, prefix: str) -> str:
    links = []
    for target, label in TOP_NAV:
        current = ' aria-current="page"' if target == slug else ""
        links.append(f'<a href="{prefix}docs/{target}.html"{current}>{label}</a>')
    return f"""<header class="topbar">
  <div class="topbar__inner">
    <button class="iconbtn navburger" type="button" aria-label="Open navigation"
            aria-expanded="false" aria-controls="sidebar">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M3 6h18M3 12h18M3 18h18"/></svg>
    </button>
    <a class="brand" href="{prefix}index.html">
      {BRAND_MARK}<span class="brand__text">Test&nbsp;Hub</span>
      <span class="brand__tag">docs</span>
    </a>
    <nav class="navlinks" aria-label="Sections">{"".join(links)}</nav>
    <div class="topbar__spacer"></div>
    {searchbtn(prefix)}
    <a class="iconbtn" href="{REPO}" rel="noopener" title="Source on GitHub"
       aria-label="Source on GitHub">{GITHUB_GLYPH}</a>
    <button class="iconbtn themetoggle" type="button" aria-label="Switch theme"
            aria-pressed="false" title="Switch theme">
      <svg class="themetoggle__sun" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="1.9" stroke-linecap="round" aria-hidden="true" focusable="false">
        <circle cx="12" cy="12" r="4.2"/>
        <path d="M12 2v2.2M12 19.8V22M4.2 4.2l1.6 1.6M18.2 18.2l1.6 1.6M2 12h2.2M19.8 12H22M4.2 19.8l1.6-1.6M18.2 5.8l1.6-1.6"/>
      </svg>
      <svg class="themetoggle__moon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
        <path d="M20.5 14.6A8.6 8.6 0 0 1 9.4 3.5a8.6 8.6 0 1 0 11.1 11.1z"/>
      </svg>
    </button>
  </div>
</header>
<div class="navscrim" hidden></div>
"""


def sidebar(slug: str, prefix: str) -> str:
    out = ['<aside class="sidebar" id="sidebar"><nav aria-label="Documentation">']
    for label, items in SIDEBAR:
        out.append('<div class="sidebar__group">')
        out.append(f'<span class="sidebar__label">{label}</span>')
        for target, title, icon in items:
            active = " is-active" if target == slug else ""
            current = ' aria-current="page"' if target == slug else ""
            path = ICONS.get(icon, ICONS["code"])
            out.append(
                f'<a class="sidebar__link{active}" href="{prefix}docs/{target}.html"{current}>'
                '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
                'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
                f'focusable="false">{path}</svg><span>{title}</span></a>'
            )
        out.append("</div>")
    out.append("</nav></aside>")
    return "\n".join(out)


# ------------------------------------------------------------- code blocks

CODEBLOCK_OPEN_RE = re.compile(r'<div class="codeblock"((?:\s+data-[a-z]+="[^"]*")*)\s*>')
ATTR_RE = re.compile(r'data-([a-z]+)="([^"]*)"')


def expand_codeblocks(body: str) -> str:
    """Give every ``<div class="codeblock">`` its header bar.

    Fragments write the compact form::

        <div class="codeblock" data-lang="shell" data-caption="Install">
        <pre><code>...</code></pre>
        </div>

    and this puts the language chip and caption into the built page. The *copy
    button* is not added here — it is created by ``site.js`` — because copying
    needs ``navigator.clipboard``, and a button baked into static HTML that
    silently does nothing when scripting is off is worse than no button.
    """

    def replace(match: re.Match[str]) -> str:
        attrs = dict(ATTR_RE.findall(match.group(1) or ""))
        lang = attrs.get("lang", "")
        caption = attrs.get("caption", "")
        keep = "".join(f' data-{k}="{v}"' for k, v in attrs.items())
        head_bits = []
        if lang:
            head_bits.append(f'<span class="codeblock__lang">{lang}</span>')
        if caption:
            head_bits.append(f'<span class="codeblock__cap">{caption}</span>')
        header = f'<div class="codeblock__head">{"".join(head_bits)}</div>' if head_bits else ""
        return f'<div class="codeblock"{keep}>{header}'

    return CODEBLOCK_OPEN_RE.sub(replace, body)


# --------------------------------------------------------- table of contents

HEADING_RE = re.compile(r'<h([23])([^>]*?)\sid="([^"]+)"([^>]*)>(.*?)</h\1>', re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")


def build_toc(body: str) -> str:
    """The "on this page" rail, from the h2/h3 ids in the fragment.

    Past twenty entries the h3s are dropped: a reference page can carry sixty
    headings, and listing all of them turns the contents into a second document
    you have to read in order to use the first one.
    """
    items = []
    for match in HEADING_RE.finditer(body):
        text = re.sub(r"\s+", " ", TAG_RE.sub("", match.group(5))).strip()
        items.append((match.group(1), match.group(3), text))
    if len(items) < 2:
        return ""
    if len(items) > 20:
        top = [item for item in items if item[0] == "2"]
        if len(top) >= 3:
            items = top
    rows = "\n".join(
        f'<li class="lvl-{level}"><a href="#{anchor}">{text}</a></li>'
        for level, anchor, text in items
    )
    return f"""<aside class="toc" aria-label="On this page">
  <div class="toc__title">On this page</div>
  <ul>
{rows}
  </ul>
</aside>"""


def pagenav(slug: str, prefix: str) -> str:
    if slug not in ORDER:
        return ""
    index = ORDER.index(slug)
    previous = ORDER[index - 1] if index > 0 else None
    following = ORDER[index + 1] if index < len(ORDER) - 1 else None
    parts = ['<nav class="pagenav" aria-label="Pagination">']
    if previous:
        parts.append(
            f'<a class="pagenav__prev" href="{prefix}docs/{previous}.html">'
            f"<span>&larr;&nbsp;Previous</span><b>{TITLES[previous]}</b></a>"
        )
    else:
        parts.append("<span></span>")
    if following:
        parts.append(
            f'<a class="pagenav__next" href="{prefix}docs/{following}.html">'
            f"<span>Next&nbsp;&rarr;</span><b>{TITLES[following]}</b></a>"
        )
    else:
        parts.append("<span></span>")
    parts.append("</nav>")
    return "\n".join(parts)


def footer(prefix: str) -> str:
    columns = []
    for label, items in SIDEBAR:
        rows = "".join(
            f'<li><a href="{prefix}docs/{target}.html">{title}</a></li>'
            for target, title, _ in items
        )
        columns.append(f"<div><h2>{label}</h2><ul>{rows}</ul></div>")
    return f"""<footer class="footer">
  <div class="footer__grid">
    <div class="footer__about">
      <a class="brand" href="{prefix}index.html">{BRAND_MARK}<span class="brand__text">Test&nbsp;Hub</span></a>
      <p>{TAGLINE}</p>
      <p class="footer__links">
        <a href="{REPO}" rel="noopener">GitHub</a> ·
        <a href="{PYPI}" rel="noopener">PyPI</a> ·
        <a href="{REPO}/issues" rel="noopener">Issues</a>
      </p>
    </div>
    {"".join(columns)}
  </div>
  <div class="footer__bottom">
    <span>Apache-2.0 licensed.</span>
    <span>This site loads nothing from the internet — same rule as the dashboard.</span>
  </div>
</footer>
<script src="{prefix}assets/js/site.js"></script>
</body>
</html>
"""


FRONT_RE = re.compile(r"^\s*<!--META\s*(\{.*?\})\s*-->", re.S)


def render(path: Path) -> tuple[str, str, list[dict]]:
    raw = path.read_text(encoding="utf-8")
    match = FRONT_RE.match(raw)
    if not match:
        raise SystemExit(f"{path.name}: missing <!--META {{...}} --> front-matter block")
    try:
        meta = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path.name}: front matter is not valid JSON: {exc}") from exc
    for required in ("title", "description"):
        if required not in meta:
            raise SystemExit(f"{path.name}: front matter has no {required!r}")

    slug = path.stem
    body = expand_codeblocks(raw[match.end() :].strip())
    prefix = "../"

    hero = f"""<div class="crumbs">
  <a href="{prefix}index.html">Home</a>
  <span aria-hidden="true">/</span>
  <span>{meta["title"]}</span>
</div>
<header class="dochero">
  <span class="eyebrow">{meta.get("eyebrow", "Documentation")}</span>
  <h1>{meta["title"]}</h1>
  <p class="lede">{meta.get("lede", meta["description"])}</p>
</header>"""

    html = (
        head(meta, prefix, slug=slug)
        + topbar(slug, prefix)
        + '<div class="docs">\n'
        + sidebar(slug, prefix)
        + '\n<main class="docmain" id="main" tabindex="-1">\n'
        + hero
        + "\n"
        + body
        + "\n"
        + pagenav(slug, prefix)
        + "\n</main>\n"
        + build_toc(body)
        + "\n</div>\n"
        + footer(prefix)
    )
    return slug, html, search_records(slug, meta, body)


# ------------------------------------------------------------- search index

#: Past this a section record stops earning its download. The heading and the
#: opening of the section are what a search result actually shows.
SNIPPET_MAX = 800

DROP_RE = re.compile(r"<(script|style|svg)\b.*?</\1>", re.S | re.I)
CODE_RE = re.compile(r"<code[^>]*>(.*?)</code>", re.S | re.I)
#: A flag, a setting, an endpoint, a module path — what a reader types verbatim.
TOKEN_RE = re.compile(r"^[-/]{0,2}[A-Za-z0-9_][A-Za-z0-9_./:-]{2,44}$")


def plain(html: str) -> str:
    """Everything a reader would see in this fragment, as one line of text."""
    text = DROP_RE.sub(" ", html)
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", unescape(text))
    # Stripping tags leaves a gap wherever markup hugged its punctuation —
    # "results.xml , and" — which a search snippet would then show verbatim.
    return re.sub(r" ([,.;:!?)\]])", r"\1", text).strip()


def keywords(chunk: str) -> str:
    """The code tokens in one section.

    A CLI reference documents its flags in tables, not in headings, so without
    this, searching for ``--insecure-no-auth`` finds every page that mentions it
    in passing and never the page that defines it.
    """
    seen: dict[str, None] = {}
    for raw in CODE_RE.findall(chunk):
        token = plain(raw)
        if not TOKEN_RE.match(token):
            continue
        # A word with no punctuation is prose in a monospace font, not a name.
        if token.isalpha() and not token.startswith("-"):
            continue
        seen.setdefault(token.lower(), None)
        if len(seen) >= 40:
            break
    return " ".join(seen)


def search_records(slug: str, meta: dict, body: str) -> list[dict]:
    """One record per section — a hit should land on the heading it matched."""
    title = unescape(meta["title"])
    page = f"docs/{slug}.html"
    marks = list(HEADING_RE.finditer(body))
    intro_html = body[: marks[0].start()] if marks else body

    records = [
        {
            "p": page,
            "t": title,
            "h": "",
            "a": "",
            "x": (plain(meta.get("lede", meta["description"])) + " " + plain(intro_html)).strip()[
                :SNIPPET_MAX
            ],
            "k": keywords(intro_html),
        }
    ]
    for index, match in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        heading = plain(match.group(5))
        if not heading:
            continue
        chunk = body[match.end() : end]
        records.append(
            {
                "p": page,
                "t": title,
                "h": heading,
                "a": match.group(3),
                "x": plain(chunk)[:SNIPPET_MAX],
                "k": keywords(chunk),
            }
        )
    return records


def write_if_changed(path: Path, payload: str, *, check: bool, label: str | None = None) -> bool:
    """Write ``payload`` unless it is already on disk. True when up to date."""
    name = label or str(path.relative_to(ROOT))
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        print(f"  = {name}")
        return True
    if check:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    print(f"  + {name}  ({len(payload):,} bytes)")
    return True


# ----------------------------------------------------------------- favicon

#: Generated rather than committed as a binary, so the whole site is reviewable
#: as text and there is no asset to forget in a copy. The bars are the product's
#: own status language — pass, accent, skip, fail — and they are drawn with
#: literal colours rather than `currentColor` because a favicon is rendered by
#: the browser chrome, where none of this page's CSS applies.
FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
<rect width="24" height="24" rx="5" fill="#111621"/>
<rect x="3.5" y="13" width="3.6" height="7.5" rx="1.1" fill="#45c98d"/>
<rect x="8.6" y="7.5" width="3.6" height="13" rx="1.1" fill="#8ba4ff"/>
<rect x="13.7" y="10.5" width="3.6" height="10" rx="1.1" fill="#e5a83c"/>
<rect x="18.8" y="4" width="2.2" height="16.5" rx="1.1" fill="#ff7a72"/>
</svg>
"""


# ------------------------------------------------------------ landing page

#: (slug, blurb) for the cards on the landing page. The blurb is not the page's
#: own description: on a card it has to say why you would click, which is a
#: different sentence from the one that belongs at the top of the page.
LANDING_CARDS = [
    (
        "getting-started",
        "rocket",
        "Install it, seed it with sample data, and have a dashboard open in about a minute.",
    ),
    (
        "rest-api",
        "server",
        "Every endpoint, with a <code>curl</code> for each. The API is the product; the SDK is a convenience.",
    ),
    (
        "adapters",
        "plug",
        "JUnit XML and Test Hub JSON ship in the box. New formats are entry-point plugins.",
    ),
    (
        "metrics",
        "chart",
        "Pass rate, failure rate, duration and flakiness — the exact formulas, and what they exclude.",
    ),
    (
        "security",
        "shield",
        "Loopback by default, hashed tokens, entity-free XML parsing, and a CSP of <code>default-src 'none'</code>.",
    ),
    (
        "deployment",
        "box",
        "Docker, Compose, a reverse proxy on a sub-path, backups, and what to check before exposing it.",
    ),
]

HERO_SNIPPET = """<div class="codeblock" data-lang="shell" data-caption="From nothing to a dashboard">
<pre><code>$ pip install test-hub
$ test-hub demo          # 5 projects, two weeks of history, real failures
$ test-hub serve         # http://127.0.0.1:8000</code></pre>
</div>"""


def landing() -> str:
    """The front page.

    Deliberately not a documentation page: no sidebar, no table of contents, no
    prev/next. It answers "what is this and why would I install it", hands over
    three commands that work, and then gets out of the way.
    """
    meta = {
        "title": SITE_TITLE,
        "description": TAGLINE,
    }
    cards = []
    for slug, icon, blurb in LANDING_CARDS:
        glyph = ICONS.get(icon, ICONS["code"])
        cards.append(
            f'<a class="card" href="docs/{slug}.html">'
            '<span class="card__icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
            f'focusable="false">{glyph}</svg></span>'
            f"<h3>{TITLES[slug]}</h3><p>{blurb}</p></a>"
        )

    body = f"""<main class="landing" id="main" tabindex="-1">
  <section class="hero">
    <div class="hero__text">
      <span class="eyebrow">Universal test observability</span>
      <h1>Every team, framework and test run<br>in one place.</h1>
      <p class="lede">Test Hub collects results from unit, component, integration, functional and
      end-to-end suites — whoever ran them, in whatever language — normalizes them into one
      canonical model, and shows them in one dashboard. Anything that can send an HTTP request can
      publish to it.</p>
      <p class="hero__cta">
        <a class="btn btn--primary" href="docs/getting-started.html">Get started</a>
        <a class="btn" href="docs/rest-api.html">REST API</a>
        <a class="btn btn--ghost" href="{REPO}" rel="noopener">Source</a>
      </p>
    </div>
    {HERO_SNIPPET}
  </section>

  <section class="strip" aria-label="What it does">
    <div class="strip__item"><b>Two products, one dashboard</b><span>A live service, and a single
      self-contained HTML file. The same rendering code behind two data providers.</span></div>
    <div class="strip__item"><b>Retries are attempts, not overwrites</b><span>A rerun is recorded
      beside the failure it followed, which is the only reason flakiness is measurable.</span></div>
    <div class="strip__item"><b>No Node, no CDN</b><span>Hand-written CSS and JavaScript inside the
      wheel. Nothing is fetched from the internet — including this site.</span></div>
  </section>

  <section class="cardgrid">
    {"".join(cards)}
  </section>

  <section class="modes">
    <h2>Two ways to read your results</h2>
    <p>Both are the same dashboard. The only thing that differs is where the data comes from, and
    that difference lives in exactly one interface.</p>
    <div class="tablewrap"><table>
      <thead><tr><th>&nbsp;</th><th>Snapshot</th><th>Live</th></tr></thead>
      <tbody>
        <tr><th scope="row">Command</th><td><code>test-hub export</code></td><td><code>test-hub serve</code></td></tr>
        <tr><th scope="row">Output</th><td>one self-contained <code>.html</code></td><td>a service on a port</td></tr>
        <tr><th scope="row">Needs a server to read</th><td>no</td><td>yes</td></tr>
        <tr><th scope="row">Updates while you watch</th><td>no</td><td>yes, over SSE</td></tr>
        <tr><th scope="row">Data provider</th><td><code>SnapshotDataProvider</code></td><td><code>LiveDataProvider</code></td></tr>
        <tr><th scope="row">Dashboard code</th><td><b>identical</b></td><td><b>identical</b></td></tr>
      </tbody>
    </table></div>
  </section>

  <section class="closing">
    <h2>Start where you are</h2>
    <div class="closing__grid">
      <div>
        <h3>You already have JUnit XML</h3>
        <p>Almost every runner can write it. Point <a href="docs/cli-reference.html#ingest"><code>test-hub
        ingest</code></a> at the file, or <code>POST</code> it to
        <a href="docs/rest-api.html#imports"><code>/api/v1/imports</code></a> from your pipeline.</p>
      </div>
      <div>
        <h3>Your framework writes nothing useful</h3>
        <p>Write <a href="docs/canonical-schema.html">Test Hub's own JSON</a> from a twenty-line
        script, or publish results as they happen over
        <a href="docs/events.html">the event API</a>.</p>
      </div>
      <div>
        <h3>Your format deserves first-class support</h3>
        <p><a href="docs/adapters.html">Write an adapter.</a> It is an entry-point plugin, and the
        two built-in adapters are registered exactly the same way.</p>
      </div>
    </div>
  </section>
</main>
"""
    return head(meta, "", slug="__index__") + topbar("", "") + body + footer("")


# ------------------------------------------------------- search index files


def write_search_index(records: list[dict], *, check: bool) -> bool:
    """Write the search index in the two forms the reader might need.

    ``assets/search-index.json`` is the artifact — it is what the page fetches
    when the site is served over HTTP, and it is a plain document anything else
    can consume.

    ``assets/search-index.js`` is the same payload assigned to a global. It
    exists because ``fetch()`` of a local file is blocked as a cross-origin
    request in every Chromium-based browser, so over ``file://`` — which is
    exactly how somebody reads these pages from a cloned repository on a plane —
    the JSON is unreachable and search would be dead. ``site.js`` tries the
    fetch first and injects the script only when it fails.

    Both are written here, from one list, in one function, so they cannot drift.
    """
    payload = json.dumps({"v": 1, "d": records}, ensure_ascii=False, separators=(",", ":"))
    ok = write_if_changed(ROOT / "assets" / "search-index.json", payload + "\n", check=check)
    shim = f"window.TEST_HUB_DOCS_SEARCH = {payload};\n"
    ok = write_if_changed(ROOT / "assets" / "search-index.js", shim, check=check) and ok
    return ok


# --------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    check = "--check" in argv
    sources = {path.stem: path for path in CONTENT.glob("*.html")}

    missing = [slug for slug in ORDER if slug not in sources]
    if missing:
        print(f"! no content file for: {', '.join(missing)}", file=sys.stderr)
        return 1
    stray = sorted(set(sources) - set(ORDER))
    if stray:
        # A fragment nobody navigates to is a page nobody reads. Fail rather than
        # build an orphan: it is always either a typo in the slug or a missing
        # SIDEBAR entry, and both are one line to fix.
        print(f"! content file with no SIDEBAR entry: {', '.join(stray)}", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    records: list[dict] = []

    for slug in ORDER:
        _, html, page_records = render(sources[slug])
        records.extend(page_records)
        if not write_if_changed(OUT / f"{slug}.html", html, check=check):
            stale.append(f"docs/{slug}.html")

    if not write_if_changed(ROOT / "index.html", landing(), check=check):
        stale.append("index.html")
    if not write_if_changed(ROOT / "assets" / "favicon.svg", FAVICON, check=check):
        stale.append("assets/favicon.svg")
    if not write_search_index(records, check=check):
        stale.append("assets/search-index.*")

    if check and stale:
        print(f"\nout of date: {', '.join(stale)}", file=sys.stderr)
        print("run `python3 site/tools/build.py` and commit the result", file=sys.stderr)
        return 1

    total = sum((OUT / f"{slug}.html").stat().st_size for slug in ORDER)
    total += (ROOT / "index.html").stat().st_size
    print(
        f"\n{len(ORDER)} pages + landing, {len(records)} search sections, "
        f"{total / 1024:,.0f} KB of HTML"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
