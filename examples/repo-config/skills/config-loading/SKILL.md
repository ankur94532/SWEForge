---
name: config-loading
description: Parse release configuration strictly, in order, with deterministic compatibility rules.
---

# Configuration loading

Turn a release policy document into the structures defined by `domain-model`.
Parsing is strict, ordered, and backward compatible, in that priority.

## Strict

Reject unknown keys rather than ignoring them; a typo in a policy file must
fail loudly at load time instead of silently disabling a required check.
Validate types and ranges at the boundary so nothing downstream re-validates.

## Ordered

Declaration order in the document is authoritative. Checks are evaluated and
reported in the order the operator wrote them, so parsing must preserve that
order rather than normalizing into a map and losing it.

## Backward compatible

An older policy file must keep loading against a newer parser. When a field
gains a richer form, accept both and normalize to the new one; record the
defaulting decision so `reporting` can explain it.

When the compatibility requirement for a change is genuinely ambiguous — two
readings would produce different, both-defensible behavior — use
`request_clarification` once during planning rather than guessing. Do not use
it for questions the policy files in the repository already answer.

## Evidence

Validate with `validate_release` against a real policy file in the worktree
and show the structured diagnostics, including the check count.
