"""Stable test identity — the property everything historical depends on.

If identity is not stable, every run invents a new set of tests: history is
empty, comparison finds nothing, and no test is ever flaky because no test is
ever the same test twice.
"""

from __future__ import annotations

import uuid

import pytest

# `test_identity` is aliased on import: pytest collects any module-level name
# beginning with `test_`, and would otherwise try to run the function under test
# as if it were a test case.
from test_hub.domain.identity import (
    TEST_HUB_NAMESPACE,
    identity_source,
    normalize_params,
    suite_id,
    workspace_id,
)
from test_hub.domain.identity import test_identity as compute_identity


def identity(**overrides: object) -> uuid.UUID:
    base: dict[str, object] = {
        "project": "payments-api",
        "suite_path": "tests/checkout/test_authorization.py",
        "test_name": "test_authorizes_a_valid_card",
    }
    base.update(overrides)
    return compute_identity(**base)  # type: ignore[arg-type]


def test_identity_is_deterministic_across_calls() -> None:
    assert identity() == identity()


def test_identity_is_stable_across_processes() -> None:
    """Hard-coded, because a change here silently orphans every historical test.

    If this assertion fails, the identity algorithm changed and every existing
    installation's history just detached. That needs a migration, not a new
    expected value.
    """
    assert str(identity()) == str(
        uuid.uuid5(
            TEST_HUB_NAMESPACE,
            "payments-api\x1ftests/checkout/test_authorization.py\x1f"
            "\x1ftest_authorizes_a_valid_card\x1f",
        )
    )


@pytest.mark.parametrize(
    "variation",
    [
        {"suite_path": "./tests/checkout/test_authorization.py"},
        {"suite_path": "tests\\checkout\\test_authorization.py"},
        {"test_name": "  test_authorizes_a_valid_card  "},
        {"test_name": "test_authorizes_a_valid_card"},
    ],
)
def test_cosmetic_differences_do_not_change_identity(variation: dict[str, str]) -> None:
    """Leading ./, Windows separators and stray whitespace are the same test."""
    assert identity(**variation) == identity()


@pytest.mark.parametrize(
    "variation",
    [
        {"project": "ledger-worker"},
        {"suite_path": "tests/checkout/test_capture.py"},
        {"test_name": "test_declines_an_expired_card"},
        {"class_name": "TestAuthorization"},
        {"params": {"card": "visa"}},
    ],
)
def test_meaningful_differences_change_identity(variation: dict[str, object]) -> None:
    assert identity(**variation) != identity()


def test_parameter_order_does_not_matter() -> None:
    """`(a=1, b=2)` and `(b=2, a=1)` are one test, not two."""
    assert identity(params={"a": 1, "b": 2}) == identity(params={"b": 2, "a": 1})


def test_positional_parameter_order_does_matter() -> None:
    """For positional parameters the position *is* the name."""
    assert identity(params=["chrome", 1024]) != identity(params=[1024, "chrome"])


def test_timestamps_and_line_numbers_are_not_part_of_identity() -> None:
    """The explicit non-requirement from the spec, asserted rather than assumed."""
    source = identity_source(
        project="p", suite_path="tests/t.py", class_name=None, test_name="test_x"
    )
    assert "2026" not in source
    assert ":" not in source


def test_memory_addresses_are_scrubbed_from_parameters() -> None:
    """A repr'd object would otherwise mint a new test on every single run."""
    first = normalize_params("<Account object at 0x7f3c1a2b3c4d>")
    second = normalize_params("<Account object at 0x7ffe9988aabb>")
    assert first == second


def test_uuids_in_parameters_are_scrubbed() -> None:
    first = normalize_params({"order": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"})
    second = normalize_params({"order": "1c2e5f6a-9999-4aaa-8bbb-2b0d7b3dcb6d"})
    assert first == second


def test_distinct_parameters_still_collide_only_when_they_should() -> None:
    assert normalize_params({"browser": "chrome"}) != normalize_params({"browser": "firefox"})


def test_rendered_parameter_string_is_accepted() -> None:
    """Adapters that only see `[chrome-1024x768]` must still get a stable id."""
    assert normalize_params("[chrome-1024x768]") == "chrome-1024x768"


def test_unicode_normalization() -> None:
    """The same name typed two ways is the same test."""
    precomposed = identity(test_name="test_café")
    combining = identity(test_name="test_café")
    assert precomposed == combining


def test_suite_and_workspace_ids_are_namespaced_and_stable() -> None:
    assert suite_id(project="p", suite_path="tests/t.py") == suite_id(
        project="p", suite_path="./tests/t.py"
    )
    assert workspace_id("default") == workspace_id("Default")
    assert suite_id(project="p", suite_path="tests/t.py") != identity(
        project="p", suite_path="tests/t.py", test_name="tests/t.py"
    )


def test_identity_source_is_documented_and_reproducible() -> None:
    """A publisher in another language must be able to reproduce this exactly."""
    source = identity_source(
        project="payments-api",
        suite_path="tests/t.py",
        class_name="TestCard",
        test_name="test_pay",
        params={"amount": 100},
    )
    assert source == "payments-api\x1ftests/t.py\x1fTestCard\x1ftest_pay\x1famount=100"
    assert len(source.split("\x1f")) == 5
