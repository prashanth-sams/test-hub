"""Static guarantees about the shipped dashboard.

These do not open a browser — they assert the invariants the dashboard is built
on, so that a change which quietly breaks one fails in CI rather than in
somebody's dashboard. The browser-level checks (rendering, contrast,
keyboard order, the 375px layout) are documented in ``docs/architecture.md``
and run with Playwright against a live server; these are the cheap guards that
run everywhere.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from test_hub.api.app import web_root

WEB = web_root()
MANIFEST = json.loads((WEB / "assets" / "manifest.json").read_text(encoding="utf-8"))
JS_FILES = [WEB / relative for relative in MANIFEST["js"]]
CSS_FILES = [WEB / relative for relative in MANIFEST["css"]]
INDEX = (WEB / "index.html").read_text(encoding="utf-8")

#: index.html carries a long explanatory comment that mentions `<script>` and
#: `<style>` by name. Strip comments before asserting on markup, or the tests
#: fail on the documentation that explains why they pass.
INDEX_MARKUP = re.sub(r"<!--.*?-->", "", INDEX, flags=re.S)


def js_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in JS_FILES)


# ------------------------------------------------------------------ shipping


def test_every_manifest_asset_is_packaged() -> None:
    for path in [*JS_FILES, *CSS_FILES]:
        assert path.is_file(), f"{path} is listed in the manifest but not shipped"


def test_index_loads_exactly_the_manifest_scripts_in_order() -> None:
    """index.html and the manifest are two lists that must not drift apart."""
    referenced = re.findall(r'<script src="([^"]+)"></script>', INDEX_MARKUP)
    assert referenced == MANIFEST["js"]


def test_index_loads_exactly_the_manifest_stylesheets() -> None:
    referenced = re.findall(r'<link rel="stylesheet" href="([^"]+)">', INDEX_MARKUP)
    assert referenced == MANIFEST["css"]


# -------------------------------------------------------------- no remote deps


def test_nothing_is_loaded_from_the_network() -> None:
    """No CDN, no webfonts. A Test Hub behind a firewall, or on a machine with no
    route to the internet, has to render exactly like one that is online."""
    for match in re.finditer(r'(?:src|href)\s*=\s*"([^"]+)"', INDEX_MARKUP):
        url = match.group(1)
        assert not url.startswith(("http://", "https://", "//")), url


def test_no_webfonts_or_remote_imports_in_css() -> None:
    for path in CSS_FILES:
        source = path.read_text(encoding="utf-8")
        assert "@import" not in source
        assert "@font-face" not in source
        assert "https://" not in source


def test_scripts_are_classic_not_modules() -> None:
    """One namespace, no module graph, no bundler: the dashboard is served as the
    files that are in the repository, and the load order is the manifest."""
    assert 'type="module"' not in INDEX_MARKUP
    source = js_source()
    assert not re.search(r"^\s*import\s+.*\bfrom\b", source, re.M)
    assert not re.search(r"^\s*export\s+(default|const|function|class)\b", source, re.M)


# ------------------------------------------------------------------- security


def test_no_inline_scripts_or_styles_in_the_served_page() -> None:
    """The CSP is `script-src 'self'; style-src 'self'` — inline would break it."""
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", INDEX_MARKUP)
    assert "<style" not in INDEX_MARKUP
    assert not re.search(
        r'\son(?:click|load|error|submit|change|input|mouse[a-z]+)\s*=\s*"', INDEX_MARKUP
    ), "inline event handler in index.html"


def test_no_inline_event_handlers_are_generated() -> None:
    source = js_source()
    assert not re.search(r"""\bon(click|load|error|mouseover)\s*=\s*['"]""", source)


def test_no_eval_or_function_constructor() -> None:
    source = js_source()
    assert "eval(" not in source
    assert "new Function(" not in source
    assert "document.write" not in source


def test_untrusted_data_is_never_written_through_innerhtml() -> None:
    """Test names and stack traces are attacker-controlled; they go in as text."""
    for path in JS_FILES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "innerHTML" in line and not line.lstrip().startswith(("*", "//", "/*")):
                pytest.fail(f"{path.name}:{number} assigns innerHTML: {line.strip()}")


# --------------------------------------------------------------------- theme


def test_both_theme_token_blocks_exist() -> None:
    css = CSS_FILES[0].read_text(encoding="utf-8")
    assert re.search(r"^:root\s*\{", css, re.M), "no light token block"
    assert re.search(r'^:root\[data-theme="dark"\]\s*\{', css, re.M), "no dark token block"
    assert "color-scheme" in css


def test_the_theme_is_resolved_before_first_paint() -> None:
    """A dark reader must never see a white flash. The bootstrap runs in 00-util.js."""
    boot = JS_FILES[0].read_text(encoding="utf-8")
    assert "prefers-color-scheme" in boot
    assert "data-theme" in boot
    assert MANIFEST["theme_storage_key"] in boot


def test_storage_access_is_guarded() -> None:
    """localStorage throws outright on a file:// origin in some browsers."""
    boot = JS_FILES[0].read_text(encoding="utf-8")
    for call in ("localStorage.getItem", "localStorage.setItem"):
        index = boot.find(call)
        assert index != -1, f"{call} not found"
        assert "try" in boot[max(0, index - 200) : index], f"{call} is not inside a try block"


def test_type_sizes_come_from_the_scale() -> None:
    """Dense UI drifts small one hint at a time until half the page is 11px.

    Every size the dashboard uses for text is a `--fs-*` token, so the drift is
    visible in review and the whole scale can be moved in one edit. The only
    literals allowed are the display numbers — a 42px hero figure is a one-off,
    not a step on a scale — and they are all comfortably large.
    """
    css = CSS_FILES[0].read_text(encoding="utf-8")

    for token in ("--fs-3xs", "--fs-2xs", "--fs-xs", "--fs-sm", "--fs-md", "--fs-lg"):
        assert re.search(rf"^\s+{token}: \d", css, re.M), f"{token} is not defined"

    offenders = []
    for number, line in enumerate(css.splitlines(), 1):
        match = re.search(r"font-size:\s*([0-9.]+)px", line)
        if match and float(match.group(1)) < 20:
            offenders.append(f"{number}: {line.strip()[:70]}")
    assert not offenders, "text sized off the scale:\n" + "\n".join(offenders)


def test_colour_literals_live_only_in_the_token_blocks() -> None:
    """Every colour must be a custom property, or the dark theme has holes in it."""
    css = CSS_FILES[0].read_text(encoding="utf-8")
    lines = css.splitlines()

    inside: list[tuple[int, int]] = []
    for match in re.finditer(r"^:root[^{]*\{", css, re.M):
        start_line = css[: match.start()].count("\n")
        depth, index = 0, match.end() - 1
        while index < len(css):
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        inside.append((start_line, css[:index].count("\n")))

    offenders = []
    for number, line in enumerate(lines):
        if line.lstrip().startswith(("*", "/*", "//")):
            continue
        if re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", line) and not any(
            start <= number <= end for start, end in inside
        ):
            offenders.append(f"{number + 1}: {line.strip()[:70]}")
    assert not offenders, "colours outside the token blocks:\n" + "\n".join(offenders)


def test_screen_reader_tables_cannot_widen_the_page() -> None:
    """Regression: a chart's visually-hidden data table pushed a 375px viewport
    into horizontal scroll, because a table under auto layout grows past its
    declared width."""
    css = CSS_FILES[0].read_text(encoding="utf-8")
    assert "table-layout: fixed" in css
    sr_only = css[css.index(".sr-only {") : css.index(".sr-only {") + 400]
    assert "position: absolute" in sr_only
    assert "max-width: 1px" in sr_only


def test_reduced_motion_is_respected() -> None:
    css = CSS_FILES[0].read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in css


# ------------------------------------------------------------------ providers


def test_the_provider_seam_is_intact() -> None:
    """ADR-0001 D1, narrowed by ADR-0003: one dashboard, one provider, and the
    seam still separating them.

    The seam outlived the second implementation on purpose — it is what keeps
    `fetch` out of the views and the metric arithmetic in one file."""
    interface = (WEB / "assets/js/10-provider.js").read_text(encoding="utf-8")
    live = (WEB / "assets/js/12-live-provider.js").read_text(encoding="utf-8")

    for method in ("getOverview", "listRuns", "getRun", "getTest"):
        assert method in interface, f"{method} missing from the provider interface"
        assert method in live, f"{method} missing from the live provider"

    # `subscribe` and the status field come from a shared base rather than from
    # the transport, so the connection pill has one definition.
    assert "function createBase(" in interface
    assert "subscribe:" in interface
    assert "createBase(" in live, "the provider does not use the shared base"


def test_only_the_provider_touches_the_network() -> None:
    """Everything that talks to the server is in one file, and it is not a view."""
    live = (WEB / "assets/js/12-live-provider.js").read_text(encoding="utf-8")
    assert "EventSource" in live
    assert "Last-Event-ID" in live or "lastEventId" in live

    interface = (WEB / "assets/js/10-provider.js").read_text(encoding="utf-8")
    assert "fetch(" not in interface, "the seam itself must not know a transport"


def test_views_do_not_call_fetch_directly() -> None:
    """Views render from a plain object graph, never from a response."""
    for path in JS_FILES:
        if not re.match(r"\d+-view-", path.name) and "components" not in path.name:
            continue
        source = path.read_text(encoding="utf-8")
        assert "fetch(" not in source, f"{path.name} bypasses the provider seam"
        assert "EventSource" not in source, f"{path.name} bypasses the provider seam"


# ------------------------------------------------------------------------ nav


def test_the_primary_nav_lives_in_the_left_rail() -> None:
    """The routes moved out of the header into a rail, so the header no longer
    has to fit brand, tabs and status in one row."""
    rail = re.search(r'<aside class="sidebar">(.*?)</aside>', INDEX_MARKUP, re.S)
    assert rail, "no sidebar in the shell"
    assert '<nav class="nav"' in rail.group(1), "the nav is not in the rail"
    assert 'class="brand"' in rail.group(1), "the brand is not in the rail"

    css = CSS_FILES[0].read_text(encoding="utf-8")
    assert ".shell__body" in css, "the content column has no styles"
    assert re.search(r"^\.nav \{[^}]*flex-direction: column", css, re.M), (
        "the rail's nav is not vertical"
    )


def test_the_rail_becomes_a_bar_on_a_phone() -> None:
    """216px of permanent column is a quarter of a phone's width, spent on two
    links."""
    css = CSS_FILES[0].read_text(encoding="utf-8")
    block = css[css.index("@media (max-width: 860px)") :]
    block = block[: block.index("@media (max-width: 640px)")]
    assert ".sidebar" in block, "the rail is still a column on a phone"


def test_every_rail_link_has_a_view_and_a_route() -> None:
    """A link in the rail that no view answers falls back to the overview and
    looks like the page simply forgot to change — the worst kind of broken."""
    routes = re.findall(r'data-route="([^"]+)"', INDEX_MARKUP)
    assert len(routes) == len(set(routes)), "a route is in the rail twice"

    app = (WEB / "assets/js/90-app.js").read_text(encoding="utf-8")
    views = set(re.findall(r"^\s+(\w+): function \(\) \{ return TestHub\.views\.", app, re.M))
    store = store_source()

    for route in routes:
        assert route in views, f"the rail links to {route}, which no view answers"
        parses = (
            # …an explicit branch, a member of the shared-dataset route table, or
            # `overview`, which is what an unrecognised hash falls back to.
            f"head === '{route}'" in store
            or re.search(rf"^\s+{route}: true,?$", store, re.M) is not None
            or route == "overview"
        )
        assert parses, f"the store cannot parse #/{route}"


def test_every_registered_view_is_defined_somewhere() -> None:
    """The router looks views up by name at mount time, so a missing one is a
    runtime error on a click rather than anything a load catches."""
    app = (WEB / "assets/js/90-app.js").read_text(encoding="utf-8")
    source = js_source()
    for name in re.findall(r"^\s+(\w+): function \(\) \{ return TestHub\.views\.", app, re.M):
        assert f"TestHub.views.{name} = {{" in source, f"no view module defines {name}"


def test_the_rail_is_dark_in_both_themes() -> None:
    """The rail is chrome: it holds still while the content switches theme. That
    only works if the light block defines the same rail tokens as the dark one —
    a rail painted with --surface would follow the theme instead."""
    css = CSS_FILES[0].read_text(encoding="utf-8")
    light = css[css.index(":root {") : css.index(':root[data-theme="dark"] {')]
    dark = css[css.index(':root[data-theme="dark"] {') :]
    for token in ("--rail-bg", "--rail-text", "--rail-muted", "--rail-active-bg"):
        assert token in light, f"{token} is not defined for the light theme"
        assert token in dark, f"{token} is not defined for the dark theme"
    assert "background: var(--rail-bg)" in css


def test_wide_content_scrolls_inside_its_own_box() -> None:
    """Regression: a card holding a nine-column table refused to be narrower than
    that table and pushed the whole page into horizontal scroll, because a grid
    item's automatic minimum size is its min-content width."""
    css = CSS_FILES[0].read_text(encoding="utf-8")
    assert ".stack > * { min-width: 0; }" in css
    assert ".card { min-width: 0; }" in css


def test_the_nav_icons_come_from_the_icon_set() -> None:
    """Inline SVG in the markup is a second copy of path data to keep in step;
    `data-icon` is resolved in 40-components.js, against the rail's solid glyphs
    first and the outline set behind it — which is the fallback `railIcon`
    itself takes, so a name that is in neither would draw the inbox icon rather
    than fail."""
    assert "<svg" not in INDEX_MARKUP, "the markup draws its own icons"
    names = re.findall(r'data-icon="([^"]+)"', INDEX_MARKUP)
    assert names, "the rail's links carry no icons"
    icons = components_source()
    for name in names:
        outline = re.search(rf"^\s+{re.escape(name)}: \[", icons, re.M)
        solid = re.search(rf"^\s+'{re.escape(name)}': {{", icons, re.M)
        assert outline or solid, f"no {name} icon in either set"


# ------------------------------------------------------------------- settings


def settings_source() -> str:
    return (WEB / "assets" / "js" / "80-settings.js").read_text(encoding="utf-8")


def components_source() -> str:
    return (WEB / "assets" / "js" / "40-components.js").read_text(encoding="utf-8")


def test_the_header_hosts_the_settings_menu() -> None:
    assert 'id="th-settings"' in INDEX_MARKUP


def test_the_old_header_theme_toggle_is_gone() -> None:
    """The theme control moved into the settings menu; the standalone header
    button is gone. A stale reference to it would fail silently at runtime."""
    for path in [*JS_FILES, WEB / "index.html", CSS_FILES[0]]:
        source = path.read_text(encoding="utf-8")
        assert "th-theme" not in source, f"{path.name} still references the removed toggle"
        assert "theme-toggle" not in source, f"{path.name} still references the removed toggle"


def test_the_theme_still_has_three_modes_including_system() -> None:
    """The menu now offers a two-state switch, but the *preference* underneath
    it stays three-state: with nothing stored, `prefers-color-scheme` still
    wins, and `followSystem` is still the way back to it."""
    boot = JS_FILES[0].read_text(encoding="utf-8")
    assert "followSystem" in boot
    assert "setMode" in boot
    assert re.search(r"MODES:\s*\['system',\s*'light',\s*'dark'\]", boot)


def test_the_theme_control_is_a_single_row_in_the_rail() -> None:
    """It started as three full-width menu rows and a hint sentence, which spent
    half the settings panel on a display preference. It is now one switch at the
    foot of the rail, one click from anywhere."""
    assert 'id="th-appearance"' in INDEX_MARKUP, "the rail has nowhere to mount it"
    assert "sidebar__foot" in INDEX_MARKUP

    settings = settings_source()
    for gone in ("Dark mode", "menu__hint", "THEME_OPTIONS", "menuitemradio"):
        assert gone not in settings, f"the theme block left {gone} in the menu"

    css = CSS_FILES[0].read_text(encoding="utf-8")
    assert ".menu__hint" not in css, "the hint's styles outlived the hint"
    assert re.search(r"^\.sidebar__foot \{[^}]*margin-top: auto", css, re.M), (
        "the switch is not pinned to the foot of the rail"
    )


def test_the_switch_follows_the_painted_theme() -> None:
    """A reader who has never chosen has no stored theme, so a switch reading
    the stored value would sit at 'off' on a dark page."""
    assert "util.theme.current() === 'dark'" in components_source()


def test_the_menu_trigger_announces_itself() -> None:
    source = components_source()
    for attribute in ("aria-haspopup", "aria-expanded", "aria-controls"):
        assert attribute in source, f"the menu trigger sets no {attribute}"


def test_the_menu_panel_starts_hidden() -> None:
    """An unhidden panel is read out by a screen reader before it is opened."""
    assert re.search(r"hidden:\s*true", components_source())


def test_the_menu_implements_the_keyboard_contract() -> None:
    """A dropdown that can only be opened with a mouse is a dropdown half the
    people who need the settings cannot reach."""
    source = components_source()
    for key in ("Escape", "ArrowDown", "ArrowUp", "Home", "End", "Tab"):
        assert f"'{key}'" in source, f"the menu does not handle {key}"


def test_escape_returns_focus_to_the_trigger() -> None:
    """Without this a keyboard user who changes their mind is dropped at the
    top of the document with no way back to where they were."""
    source = components_source()
    assert "restoreFocus" in source
    assert "trigger.focus()" in source


def test_the_dark_mode_row_is_a_switch() -> None:
    """A drawn track announces nothing on its own; the role and `aria-checked`
    are what tell a screen reader this is an on/off control and which it is.
    The label is hidden on a narrow bar, so the name has to survive that."""
    source = components_source()
    assert "role: 'switch'" in source
    assert "aria-checked" in source
    assert "'aria-label': 'Dark mode'" in source


def test_the_switch_stops_listening_when_it_is_dropped() -> None:
    """It subscribes to the theme; a mount that cannot unsubscribe leaks one
    listener per mount."""
    source = components_source()
    block = source[source.index("function themeSwitch") :]
    assert "stopListening()" in block[: block.index("function menuSection")]


def test_the_menu_releases_its_document_listeners() -> None:
    """They are on `document`, so dropping the element does not collect them."""
    source = components_source()
    assert "removeEventListener('mousedown'" in source
    assert "removeEventListener('focusin'" in source


def test_the_menu_respects_reduced_motion() -> None:
    css = CSS_FILES[0].read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in css
    block = css[css.index("prefers-reduced-motion") :]
    assert "menu__panel" in block, "the menu's entry animation ignores reduced motion"


# ------------------------------------------------------------------- caching


def test_the_dashboard_is_revalidated_rather_than_cached_blind(client: Any) -> None:
    """The bug this exists to stop: `StaticFiles` sends an ETag and no
    `Cache-Control`, and a validator with no freshness directive is one a
    browser may cache heuristically for hours. The symptom is an upgraded Test
    Hub running the previous release's JavaScript against the current release's
    markup — silently, because nothing errors.
    """
    for route in ("/", "/assets/js/90-app.js", "/assets/css/app.css"):
        response = client.get(route)
        assert response.status_code == 200, route
        assert response.headers.get("cache-control") == "no-cache", route


def test_revalidation_still_returns_304(client: Any) -> None:
    """`no-cache` must not mean re-downloading the bundle on every load."""
    first = client.get("/assets/js/90-app.js")
    etag = first.headers.get("etag")
    assert etag, "no ETag to revalidate against"
    again = client.get("/assets/js/90-app.js", headers={"If-None-Match": etag})
    assert again.status_code == 304


def test_the_api_is_left_alone(client: Any) -> None:
    """The cache directive is for the dashboard. The API and SSE set their own."""
    headers = {key.lower() for key in client.get("/api/v1/projects").headers}
    assert "cache-control" not in headers


# ------------------------------------------------------------------ retention


def analytics_source() -> str:
    return (WEB / "assets" / "js" / "52-view-analytics.js").read_text(encoding="utf-8")


def store_source() -> str:
    return (WEB / "assets" / "js" / "30-store.js").read_text(encoding="utf-8")


# ------------------------------------------------------------------ analytics


def test_the_analytics_view_is_shipped_and_reachable() -> None:
    """A view nobody can navigate to is dead weight in every export."""
    assert "assets/js/52-view-analytics.js" in MANIFEST["js"]
    assert 'data-route="analytics"' in INDEX_MARKUP, "the rail has no way into the view"
    assert '"#/analytics"' in INDEX_MARKUP
    assert "analytics: function ()" in (WEB / "assets" / "js" / "90-app.js").read_text(
        encoding="utf-8"
    ), "the router does not know the view"


def test_analytics_reads_a_bounded_number_of_runs() -> None:
    """The page needs whole run payloads, which is exactly why the count has to
    be a constant somebody had to think about rather than a filter's leftovers.
    """
    source = store_source()
    match = re.search(r"var ANALYTICS_RUNS = (\d+);", source)
    assert match, "the run budget is not a named constant"
    assert 1 <= int(match.group(1)) <= 8, "that is a lot of run payloads for one page"


def test_analytics_never_classifies_a_run_that_is_still_going() -> None:
    """Half a run's failures grouped as if they were all of them is a wrong
    answer, not a partial one."""
    source = store_source()
    body = source[
        source.index("function analyticsRunsFrom") : source.index("function loadRunDetails")
    ]
    assert "is_live" in body and "finished_at" in body


def test_finished_run_details_are_cached_rather_than_refetched() -> None:
    """A finished run cannot change, so a live update on this page must not cost
    five payloads again."""
    source = store_source()
    assert "analyticsCache" in source
    assert "if (resync) { analyticsCache = {}; }" in source, "a resync must empty the cache"


def test_the_analytics_view_never_calls_fetch_itself() -> None:
    """ADR-0001 D1 again: the same view has to work over a frozen snapshot."""
    source = analytics_source()
    assert "fetch(" not in source
    assert "XMLHttpRequest" not in source


def test_every_analytics_verdict_has_a_colour_and_an_explanation() -> None:
    """Four words that look like a taxonomy have to behave like one: each is
    styled, and each says what earned it."""
    source = analytics_source()
    css = CSS_FILES[0].read_text(encoding="utf-8")
    for verdict in ("stable", "flaky", "broken"):
        assert f"return '{verdict}';" in source, f"nothing ever returns {verdict}"
        assert f'.verdict[data-verdict="{verdict}"]' in css, f"{verdict} has no colour"
    hints = source[source.index("var VERDICT_HINTS") :]
    for verdict in ("stable", "flaky", "broken", "quiet"):
        assert f"{verdict}:" in hints, f"{verdict} carries no explanation"


def test_the_run_counters_lead_somewhere() -> None:
    """The counter and the list of tests behind it are the same question asked
    twice; the counters on the run page answer it in one click."""
    run_view = (WEB / "assets" / "js" / "60-view-run.js").read_text(encoding="utf-8")
    assert "ui.statStrip" in run_view
    assert "filterTestsByStatus" in run_view
    components = components_source()
    body = components[components.index("function statStrip") :]
    body = body[: body.index("function streak")]
    assert "!opts.onSelect || !value" in body, "a counter at zero must not be a button"


def settings_view_source() -> str:
    return (WEB / "assets" / "js" / "70-view-settings.js").read_text(encoding="utf-8")


def test_the_settings_view_is_shipped() -> None:
    assert "assets/js/70-view-settings.js" in MANIFEST["js"]
    assert "#/settings" in settings_source(), "the menu has no way into the view"


def test_deleting_needs_a_preview_first() -> None:
    """The two-step flow is the safety. A delete button that could be pressed
    without the counts in front of it would be a delete button pressed blind."""
    source = settings_view_source()
    assert "if (preview === null) { return null; }" in source, "the preview card is unconditional"
    assert "invalidatePreview" in source, "changing the cutoff must throw the preview away"
    assert "confirm: true" in source


def test_changing_any_input_invalidates_the_preview() -> None:
    """Otherwise the button says "delete 30 runs" while describing a cutoff the
    reader has already changed."""
    source = settings_view_source()
    body = source[source.index("function formCard") : source.index("function previewCard")]
    for field in ("th-retention-amount", "th-retention-keep", "th-retention-unfinished"):
        assert field in body
    assert body.count("invalidatePreview") >= 4


def test_the_view_never_calls_fetch_itself() -> None:
    """ADR-0001 D1: views talk to the provider, never to the network. It is what
    lets snapshot mode answer `admin: false` instead of erroring."""
    source = settings_view_source()
    assert "fetch(" not in source
    assert "XMLHttpRequest" not in source
    for call in ("provider.getRetention", "provider.previewPrune", "provider.runPrune"):
        assert call in source


def test_the_danger_button_is_styled_after_the_base_button() -> None:
    """`.btn--danger` overrides `.btn`'s own background at the same specificity,
    so source order is the only thing that decides which wins."""
    css = CSS_FILES[0].read_text(encoding="utf-8")
    assert css.index(".btn--danger") > css.index(".btn--primary")


# ------------------------------------------------------- status by project


def overview_source() -> str:
    return (WEB / "assets" / "js" / "50-view-overview.js").read_text(encoding="utf-8")


def status_bar_classes() -> dict[str, list[str]]:
    """``STATUS_BAR_CLASSES`` from the component kit: class key -> statuses."""
    source = components_source()
    block = source[source.index("var STATUS_BAR_CLASSES") :]
    block = block[: block.index("];")]
    return {
        key: re.findall(r"'([a-z]+)'", statuses)
        for key, statuses in re.findall(r"key: '(\w+)'.*?statuses: \[([^\]]*)\]", block)
    }


def test_every_status_lands_in_exactly_one_bar_class() -> None:
    """A status no class claims would drop out of every bar, and the bars would
    stop adding up to the runs they draw — silently, for whoever has one."""
    util = JS_FILES[0].read_text(encoding="utf-8")
    order = util[util.index("var STATUS_ORDER = [") :]
    statuses = re.findall(r"'([a-z]+)'", order[: order.index("];")])
    assert len(statuses) == 10, statuses

    claimed = [status for members in status_bar_classes().values() for status in members]
    assert sorted(claimed) == sorted(statuses), "a status is in no bar class, or in two"


def test_every_status_bar_class_has_a_colour() -> None:
    css = CSS_FILES[0].read_text(encoding="utf-8")
    for key in status_bar_classes():
        assert f'.sbars__seg[data-status="{key}"]' in css, f"the {key} segment has no colour"
        assert f'.sbars__key[data-status="{key}"]' in css, f"the {key} readout key has no colour"


def test_in_progress_is_always_the_end_of_the_bar() -> None:
    """So a run still going reads as a bar filling up from the left."""
    assert list(status_bar_classes())[-1] == "running"
    assert status_bar_classes()["running"] == ["running", "queued"]


def test_the_status_row_is_built_once_and_patched_in_place() -> None:
    """Rebuilding it on each update would blink every bar on every live event
    and drop the keyboard focus of whoever is reading it."""
    source = overview_source()
    assert source.count("ui.statusBars(") == 1
    render = source[source.index("    function render(state)") :]
    render = render[: render.index("\n    }\n")]
    assert "projectStatus.update(overview)" in render
    assert "statusRow(" not in render, "the status row is rebuilt on every render"
    assert "statusSlot" in render, "the status row is not kept out of the rebuilt slots"


def test_the_status_row_never_sorts_by_its_own_numbers() -> None:
    """Rows that re-sort when a live result lands are rows nobody can follow."""
    source = overview_source()
    body = source[source.index("function statusRow") : source.index("function create(ctx)")]
    assert "entries.sort(" in body
    sort = body[body.index("entries.sort(") :]
    sort = sort[: sort.index("});")]
    assert "counts" not in sort and "pass_rate" not in sort


def test_a_projects_latest_run_reaches_the_view_normalized() -> None:
    """The row draws `ProjectHealth.last_run`, from the server or from the
    browser-side recomputation a filter triggers — both must carry it, and
    with its derived rates."""
    live = (WEB / "assets/js/12-live-provider.js").read_text(encoding="utf-8")
    assert "provider.normalizeSummary(raw.last_run)" in live
    interface = (WEB / "assets/js/10-provider.js").read_text(encoding="utf-8")
    assert "last_run: latest" in interface
    assert "last_run" in overview_source()


# --------------------------------------------------------------- live timeline


def timeline_source() -> str:
    return (WEB / "assets" / "js" / "42-timeline.js").read_text(encoding="utf-8")


def test_the_timeline_is_shipped_after_the_component_kit() -> None:
    """It builds on `ui.statusBarClasses` and friends, so it has to load after
    40-components.js and before the overview that mounts it."""
    order = MANIFEST["js"]
    assert order.index("assets/js/40-components.js") < order.index("assets/js/42-timeline.js")
    assert order.index("assets/js/42-timeline.js") < order.index("assets/js/50-view-overview.js")
    assert "ui.timeline = create;" in timeline_source()


def test_the_timeline_clock_stops_when_the_overview_goes() -> None:
    """The clock is an interval and two window listeners. A view that drops the
    element without stopping them leaks a ticking timer per visit."""
    source = timeline_source()
    destroy = source[source.index("destroy: function ()") :]
    destroy = destroy[: destroy.index("}")]
    assert "stop()" in destroy
    assert "removeEventListener('visibilitychange'" in destroy
    assert "removeEventListener('resize'" in destroy
    assert "clearInterval" in source

    overview = overview_source()
    view_destroy = overview[overview.rindex("destroy: function ()") :]
    assert "liveTimeline.destroy()" in view_destroy


def test_the_timeline_pauses_while_the_tab_is_hidden() -> None:
    source = timeline_source()
    body = source[source.index("function onVisibility") :]
    body = body[: body.index("\n    }\n")]
    assert "document.hidden" in body and "stop()" in body


def test_the_timeline_moves_by_transform_not_by_redrawing() -> None:
    """The clock writes a transform once a second and the compositor glides it;
    rebuilding the bars each second would blink them and drop focus."""
    source = timeline_source()
    assert "translateX(" in source
    css = CSS_FILES[0].read_text(encoding="utf-8")
    assert re.search(r"\.tl__col,[^{]*\{\s*transition: transform 1s linear", css), (
        "the timeline's columns do not glide between ticks"
    )
    layout = source[source.index("function layout(") : source.index("function currentBin")]
    assert "createBin(" not in layout, "a tick must reposition bars, never create them"


def test_the_timeline_is_loaded_with_the_overview_and_on_every_live_event() -> None:
    store = store_source()
    route = store[store.index("function applyRoute") : store.index("function hashWith")]
    assert "loadTimeline()" in route
    live = store[store.index("function onProviderMessage") :]
    live = live[: live.index("var store = {")]
    assert "loadTimeline()" in live, "a run that starts must reach the timeline"


def test_the_timeline_asks_the_api_for_its_window() -> None:
    """`since` goes to `GET /runs` — the API refuses one without an offset, and
    `toISOString()` always carries one."""
    store = store_source()
    body = store[store.index("function loadTimeline") : store.index("function loadRuns")]
    assert "filters.since = new Date(" in body and ".toISOString()" in body
    live = (WEB / "assets/js/12-live-provider.js").read_text(encoding="utf-8")
    assert "params.since = clean.since" in live


def test_every_timeline_span_is_offered_and_has_a_default() -> None:
    store = store_source()
    spans = re.findall(r"'(\w+)': \{ key: '(\w+)'", store)
    assert spans, "no timeline spans"
    assert all(key == inner for key, inner in spans)
    default = re.search(r"var DEFAULT_TIMELINE_SPAN = '(\w+)';", store)
    assert default and default.group(1) in {key for key, _ in spans}


def test_the_timeline_table_cannot_widen_a_phone() -> None:
    """Regression: a hidden table is at least as wide as its caption, and the
    timeline's caption is a sentence — 380px on its own."""
    source = timeline_source()
    assert "el('div', { className: 'sr-only' }, el('table'" in source
