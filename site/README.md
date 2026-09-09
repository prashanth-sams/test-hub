# The Test Hub documentation site

A static website with no build dependencies. `tools/build.py` uses only the
Python standard library, so a fresh clone can rebuild the whole site with the
Python that is already on the machine.

## How it fits together

```
site/
├── content/<slug>.html   the pages you edit — body fragments with front matter
├── tools/build.py        wraps each fragment in the shared shell
├── assets/css/site.css   every colour is a custom property, defined twice
├── assets/js/site.js     theme toggle, search, copy buttons, mobile nav
├── docs/<slug>.html      the built output — committed, served by Pages
├── index.html            the built landing page
└── assets/search-index.json   generated; drives Ctrl/Cmd-K search
```

You edit `content/`. Everything in `docs/`, `index.html` and
`assets/search-index.json` is generated — a change made there is lost on the next
build.

## Building

```shell
$ cd site
$ python3 tools/build.py            # build everything
$ python3 tools/build.py --check    # fail if the committed output is stale
```

The `--check` form is what CI runs on a pull request, because the built pages are
committed. Editing a fragment without rebuilding is the one mistake this layout
invites, and that check is how it gets caught.

## Writing a page

Each file in `content/` is a **body fragment** — no `<html>`, `<head>`, `<body>`
or `<h1>`; the shell supplies all of those. It begins with exactly one front
matter line:

```html
<!--META {"title": "Metrics", "eyebrow": "Contracts", "description": "…", "lede": "…"} -->
```

| Key | Purpose |
|---|---|
| `title` | Page heading, browser title, sidebar entry |
| `eyebrow` | Small label above the heading — usually the sidebar group |
| `description` | Meta description and search result summary |
| `lede` | The standfirst paragraph under the heading |

After that, `<h2 id="...">` sections with `<h3 id="...">` subsections. **Every
heading needs a stable kebab-case `id`** — the table of contents, the search
index and every deep link are generated from them, so changing one breaks
inbound links.

### Adding a new page

Two edits, and the build enforces both:

1. Create `content/<slug>.html`.
2. Add `(slug, "Title", "icon")` to `SIDEBAR` in `tools/build.py`.

A fragment with no sidebar entry, or a sidebar entry with no fragment, is
reported by name when you build.

## Components

Copy the markup from an existing page rather than inventing new classes — the
stylesheet only knows about these.

```html
<div class="codeblock" data-lang="shell" data-caption="Optional caption">
<pre><code>$ test-hub demo</code></pre>
</div>

<div class="note note--tip"><span class="note__title">Tip</span>…</div>
<div class="note note--warn"><span class="note__title">Careful</span>…</div>

<ol class="steps"><li><h3>Step</h3><p>…</p></li></ol>

<div class="tablewrap"><table>…</table></div>   <!-- anything wide -->
<div class="cards">…</div>                       <!-- feature grid -->
```

Escape `<`, `>` and `&` inside code blocks — several pages legitimately contain
XML and HTML, and an unescaped `</script>` in an example will end the page.

## Constraints

These are not stylistic preferences; each one is load-bearing.

- **No external origins.** No CDN, no webfonts, no analytics. The site must open
  identically from `file://` and from GitHub Pages, and CI fails on any `src` or
  `href` pointing off-origin. A system font stack is the whole typography story.
- **No build dependencies.** Standard library only. A docs build that needs
  `pip install` rots the first time somebody clones the repository on a fresh
  machine.
- **Light and dark, both designed.** Every colour in `site.css` is a custom
  property defined in `:root` and again in `:root[data-theme="dark"]`. The theme
  is resolved by a small script before first paint — a stored choice wins,
  otherwise the operating system decides — and storage access is wrapped in
  `try`/`catch` because reading `localStorage` from `file://` throws in some
  browsers.
- **Accessible.** Skip link, landmarks, visible focus rings, `aria-current` on
  the active navigation item, keyboard-reachable search, and
  `prefers-reduced-motion` respected.

## Publishing

`.github/workflows/site.yml` builds and deploys to GitHub Pages on a push to
`main` that touches `site/`. On a pull request it runs `--check`, the
external-origin scan and an internal link check, and deploys nothing.
