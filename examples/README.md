# Example repository configuration

[`repo-config/`](repo-config) is a complete, valid SWEForge repository
configuration bundle. It installs as-is:

```console
sweforge repo validate owner/repo ./examples/repo-config
sweforge repo configure owner/repo ./examples/repo-config
sweforge repo show owner/repo
```

The scenario is release readiness: decide whether a release may ship, using
repository code plus two external services.

## Layout

```
repo-config/
  workflow.yaml                              4 tasks, phases, capability grants
  skills/
    domain-model/SKILL.md                    shared vocabulary
    config-loading/SKILL.md                  strict, ordered, compatible parsing
    readiness-rules/SKILL.md                 check evaluation
    readiness-rules/checks.md                supporting file, loaded on demand
    reporting/SKILL.md                       rendering and the report artifact
    release-catalog/SKILL.md                 how to use the MCP capabilities
  tools/
    scripts/README.md                        registered script tool reference
    scripts/validate-release/                python, effect: read, one secret
    scripts/write-readiness-report/          shell, effect: mutate
    mcp/README.md                            MCP capability reference
    mcp/servers.yaml                         local stdio + remote HTTPS servers
```

## What it demonstrates

**Deterministic scheduling.** `model` runs first, `config` and `readiness` fan
out from it, `reporting` joins them. Among ready tasks, declaration order
decides; one task is active at a time.

**Capability grants per phase.** A phase's `tools` list is the complete set of
capabilities reachable in that phase. `write_readiness_report` declares
`effect: mutate`, so bundle validation refuses to let any planning or
validation phase name it — a read-only phase cannot be handed a writing tool
by mistake.

**Progressive skill discovery.** Each phase has one primary `skill`; `skills`
lists further skills the model may load when it needs them. `release-catalog`
is only reachable from the phases that actually call external services, and
`readiness-rules/checks.md` shows a skill carrying supporting files.

**Both tool kinds.** `validate_release` (python, read-only, credentialed) and
`write_readiness_report` (shell, mutating) are registered script tools staged
from the installed generation. `release_catalog` (local stdio) and
`release_service` (remote HTTPS) are MCP servers, reachable from the workflow
as `<server_id>_<tool_name>`.

**Two credential channels.** stdio MCP servers and script tools take secrets
through `secret_env`; remote MCP servers take them through `secret_headers`,
kept separate from fixed `headers`. Both hold references, never values.

## Credentials this bundle expects

```console
sweforge secret set owner/repo RELEASE_POLICY_TOKEN
sweforge secret set owner/repo RELEASE_CATALOG_TOKEN
sweforge secret set owner/repo RELEASE_SERVICE_AUTH
sweforge secret set owner/repo RELEASE_SERVICE_INTERNAL_TOKEN
sweforge secret check owner/repo
```

The bundle installs and validates without them; the tools that need them fail
closed until they are configured. Reference names are part of the
configuration digest, values are not, so rotating a value never creates a new
generation.

## Adapting it

The connection details are deliberately unreachable — the stdio command path
and the `mcp.releases.example.com` URL do not exist. Replace them, or delete
`tools/mcp/servers.yaml` and the MCP tool names in `workflow.yaml`, before
pointing this at a real repository.

Once installed, maintain it incrementally rather than rebuilding the bundle:

```console
sweforge skill add owner/repo ./examples/repo-config/skills/release-catalog
sweforge tool add owner/repo ./examples/repo-config/tools/scripts/validate-release
sweforge mcp set owner/repo ./examples/repo-config/tools/mcp/servers.yaml
sweforge workflow set owner/repo ./examples/repo-config/workflow.yaml
```

Each command creates a new immutable generation. See
[docs/adding-a-repository.md](../docs/adding-a-repository.md).
