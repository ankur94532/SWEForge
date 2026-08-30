---
name: domain-model
description: Maintain release-readiness domain structures and their backward compatibility.
---

# Domain model

The domain model is the vocabulary every other task in this workflow depends
on. `config-loading`, `readiness-rules`, and `reporting` all read the
structures defined here, so a change made carelessly here is a change made
everywhere.

## Structures

- `Release` — identity and target window of a single release.
- `Check` — one readiness question, with `required` distinguishing a blocking
  check from an advisory one.
- `CheckResult` — the outcome of evaluating a `Check`, carrying enough context
  to render a report line without re-evaluating anything.
- `ReadinessReport` — the ordered collection of `CheckResult`s plus the overall
  verdict.

## Rules

Make release state and check outcomes explicit rather than inferred. A caller
should never have to reconstruct *why* a release was not ready by comparing
fields.

Preserve public structure. Fields may be added; existing field names, types,
and meanings may not change, and a field that becomes redundant is deprecated
in place rather than removed. Downstream reporting is expected to keep working
against the previous shape.

Keep the model free of transport and presentation concerns. Rendering belongs
to `reporting`; parsing belongs to `config-loading`.

## Evidence

Validation for this task means demonstrating that existing consumers still
type-check and still round-trip their data — not merely that new code runs.
