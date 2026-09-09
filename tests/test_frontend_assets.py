"""Static guarantees about the shipped dashboard.

These do not open a browser — they assert the invariants the dashboard is built
on, so that a change which quietly breaks one fails in CI rather than in
somebody's offline report. The browser-level checks (rendering, contrast,
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
    """The server and the offline exporter must agree on the load order."""
    referenced = re.findall(r'<script src="([^"]+)"></script>', INDEX_MARKUP)
    assert referenced == MANIFEST["js"]


def test_index_loads_exactly_the_manifest_stylesheets() -> None:
    referenced = re.findall(r'<link rel="stylesheet" href="([^"]+)">', INDEX_MARKUP)
    assert referenced == MANIFEST["css"]


def test_the_exporter_markers_are_present() -> None:
    for marker in MANIFEST["markers"].values():
        assert marker in INDEX, f"{marker} is missing — the exporter would fail"


# -------------------------------------------------------------- no remote deps


def test_nothing_is_loaded_from_the_network() -> None:
    """No CDN, no webfonts. It is what makes the offline export possible."""
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
    """ES modules do not load over file://, and the offline report is a file:// page."""
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


def test_both_providers_exist_and_share_one_interface() -> None:
    """ADR-0001 D1: one dashboard, two data providers, no second renderer."""
    names = {path.name for path in JS_FILES}
    assert {"11-snapshot-provider.js", "12-live-provider.js"} <= names

    interface = (WEB / "assets/js/10-provider.js").read_text(encoding="utf-8")
    snapshot = (WEB / "assets/js/11-snapshot-provider.js").read_text(encoding="utf-8")
    live = (WEB / "assets/js/12-live-provider.js").read_text(encoding="utf-8")

    # The data methods are implemented per provider…
    for method in ("getOverview", "listRuns", "getRun", "getTest"):
        assert method in interface, f"{method} missing from the provider interface"
        assert method in snapshot, f"{method} missing from the snapshot provider"
        assert method in live, f"{method} missing from the live provider"

    # …while `subscribe` and the status field come from one shared base, so the
    # dashboard's connection pill means the same thing in both modes.
    assert "function createBase(" in interface
    assert "subscribe:" in interface
    for source, name in ((snapshot, "snapshot"), (live, "live")):
        assert "createBase(" in source, f"the {name} provider does not use the shared base"


def test_only_the_live_provider_touches_the_network() -> None:
    """The snapshot provider must work with no server; that is its entire job."""
    snapshot = (WEB / "assets/js/11-snapshot-provider.js").read_text(encoding="utf-8")
    assert "fetch(" not in snapshot
    assert "EventSource" not in snapshot

    live = (WEB / "assets/js/12-live-provider.js").read_text(encoding="utf-8")
    assert "EventSource" in live
    assert "Last-Event-ID" in live or "lastEventId" in live


def test_views_do_not_call_fetch_directly() -> None:
    """Views render from a plain object graph so both providers can feed them."""
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
    assert re.search(r"^\.nav \{[^}]*flex-direction: column", css, re.M), "the rail's nav is not vertical"


def test_the_rail_becomes_a_bar_on_a_phone() -> None:
    """216px of permanent column is a quarter of a phone's width, spent on two
    links."""
    css = CSS_FILES[0].read_text(encoding="utf-8")
    block = css[css.index("@media (max-width: 860px)") :]
    block = block[: block.index("@media (max-width: 640px)")]
    assert ".sidebar" in block, "the rail is still a column on a phone"


def test_the_nav_icons_come_from_the_icon_set() -> None:
    """Inline SVG in the markup is a second copy of path data to keep in step;
    `data-icon` is resolved against the one set in 40-components.js."""
    assert "<svg" not in INDEX_MARKUP, "the markup draws its own icons"
    names = re.findall(r'data-icon="([^"]+)"', INDEX_MARKUP)
    assert names, "the rail's links carry no icons"
    icons = components_source()
    for name in names:
        assert re.search(rf"^\s+{name}: \[", icons, re.M), f"no {name} icon in the set"


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
    block = source[source.index("function themeSwitch"):]
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


def test_a_snapshot_says_it_cannot_prune() -> None:
    """A frozen file has no server to ask; the view must render its explanation
    rather than a form that cannot work."""
    snapshot = (WEB / "assets" / "js" / "11-snapshot-provider.js").read_text(encoding="utf-8")
    assert "getRetention" in snapshot
    assert "admin: false" in snapshot


def test_the_danger_button_is_styled_after_the_base_button() -> None:
    """`.btn--danger` overrides `.btn`'s own background at the same specificity,
    so source order is the only thing that decides which wins."""
    css = CSS_FILES[0].read_text(encoding="utf-8")
    assert css.index(".btn--danger") > css.index(".btn--primary")
