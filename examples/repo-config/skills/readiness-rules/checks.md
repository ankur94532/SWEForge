# Check catalog

A supporting resource for the `readiness-rules` skill. Skill directories may
carry additional files like this one; they are installed with the skill and
loaded on demand rather than pushed into every prompt.

## Check kinds

| kind | question | typical failure |
| --- | --- | --- |
| `build` | does the release artifact build reproducibly? | toolchain drift |
| `tests` | did the required suites pass on the release commit? | flaky exclusion |
| `migrations` | are schema migrations forward and backward compatible? | destructive column drop |
| `dependencies` | are pinned dependencies resolvable and unyanked? | yanked transitive pin |
| `window` | does the target window still exist and remain open? | window closed after approval |
| `signoff` | are the recorded approvals still valid for this commit? | approval predates the commit |

## Evaluating a check

1. Resolve the check's inputs. `window` and `signoff` need the release
   catalog; the rest are answerable from the repository.
2. Evaluate once and record the outcome, the inputs it was based on, and a
   one-line reason. A `CheckResult` a reader cannot explain is not finished.
3. Never re-run a check to produce a different answer for the report.

## Required versus optional

`required: true` blocks the release. `required: false` is advisory: it appears
in the report with its outcome and reason, and the verdict ignores it.

A check whose inputs are unavailable is a failure, not a skip. Report the
missing input as the reason. Treating unavailability as success is how a
closed release window reaches production.
