---
name: reporting
description: Render backward-compatible readiness reports with stable field and check ordering.
---

# Reporting

Render the `ReadinessReport` produced by `readiness-rules`. This is the only
task that writes a report artifact, and the only one that may call the remote
release service.

## Compatibility

Report fields are consumed by tooling outside this repository. Add fields;
never rename, retype, or remove one. When a value's meaning would have to
change, add the new field alongside and leave the old one populated.

## Ordering

Render checks in declaration order, the same order `readiness-rules`
evaluated them. Do not group by outcome, and do not sort failures to the top:
a diff between two reports should show what changed, not a reshuffle.

Failed checks are rendered with their reason inline. A reader must be able to
tell why the release is not ready without opening another file.

## Writing the report

`write_readiness_report` writes into the current worktree at a relative path.
It is a mutating tool and is therefore reachable only from this task's
execution phase — planning and validation cannot call it.

Confirm the target window with `release_service_validate_release_window`
before writing a report that claims readiness. The service is authoritative
for the window; the repository is not.

## Evidence

Show the rendered report and confirm the check order matches the policy file.
When the change taught something durable about this repository's reporting
contract, record it with `propose_repo_memory` during validation.
