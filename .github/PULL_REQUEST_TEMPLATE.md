<!--
Thanks for sending this.

Delete any section that does not apply — an honest short PR description beats a
fully-filled-in template that says nothing. The checklist at the bottom is the
part reviewers actually use.
-->

## What this changes

<!-- One paragraph. What is different after this is merged, from a user's side. -->

## Why

<!--
The problem, not the patch. If there is an issue, "Fixes #123" is enough; if
there is not, say what went wrong or what was missing.
-->

## How

<!--
Only the parts a reviewer could not guess: the design decision you made, the
alternative you rejected, the bit of the diff that looks wrong until you know
why. Skip it for small changes.
-->

## Wire-contract impact

<!--
Answer this even if the answer is "none" — it is the question that costs the
most to get wrong.

Anything that changes `domain/models.py`, `domain/events.py`, a status value,
the identity hash in `domain/identity.py`, or the shape of a REST response is a
contract change. Adding an optional field is compatible; anything else needs a
`SCHEMA_VERSION` bump in `version.py` and a note in `docs/schemas/`.

A change to `identity_source()` is the sharpest edge in the project: it silently
re-identifies every historical test and throws away the history that flakiness
is computed from.
-->

- [ ] No change to the wire contract
- [ ] Additive and backwards-compatible (new optional field, new alias)
- [ ] Breaking — `SCHEMA_VERSION` bumped and the migration path documented

## How you tested it

<!--
Not "CI is green". What did you actually run, and what did you see? For an
adapter, name the fixture. For a UI change, say which browser and whether you
checked it in the snapshot export as well as live — those are two render paths
and only one of them has a server.
-->

---

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `mypy` passes (strict — no new `# type: ignore` without a reason beside it)
- [ ] `pytest` passes, and there is a test that fails without this change
- [ ] Comments explain *why*; none of them restate the line below
- [ ] Docs updated if behaviour changed (`docs/`, and `site/content/` if it is user-facing)
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]`, unless this is internal-only
- [ ] No new runtime dependency (or: it is justified above, and it is small and maintained)
- [ ] Nothing secret in the diff — no tokens, no real hostnames, no customer test names in fixtures
