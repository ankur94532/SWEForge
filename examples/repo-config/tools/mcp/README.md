# Repository MCP capabilities

`servers.yaml` is optional. When present it is the complete, trusted list of
MCP servers this repository may reach and the exact tools it may call from
each. It is installed by an operator and frozen into the configuration
generation; the target repository's checkout can never contribute to it.

## Schema

```yaml
version: 1
servers:
  <server_id>:                 # [A-Za-z][A-Za-z0-9_.-]{0,63}
    connection:                # passed to the MCP client
      transport: stdio | streamable_http | http | sse
      ...                      # transport-specific keys
    headers:                   # remote only: fixed non-secret HTTP headers
      <Header-Name>: <value>
    secret_env:                # stdio only: env name -> repository secret name
      <ENV_NAME>: <SECRET_NAME>
    secret_headers:            # remote only: header name -> secret name
      <Header-Name>: <SECRET_NAME>
    tools: [<tool_name>, ...]  # the allowlist; at least one entry
```

`stdio` servers take `command`, `args`, and an optional fixed `env`. Remote
servers take `url`. Anything not listed above is rejected at validation.

## Reaching a tool from the workflow

An approved tool is named `<server_id>_<tool_name>` wherever a workflow phase
lists its tools:

```yaml
    planning:
      skill: readiness-rules
      tools: [read_file, grep, release_catalog_lookup_release]
```

Approval and reachability are separate. A tool listed in `servers.yaml` but
named by no phase is installed and allowlisted but never offered to the model.
Every invocation is re-authorized against the allowlist, so a model that
somehow names an unapproved tool is refused rather than proxied.

## Credentials

Values live in the encrypted repository secret store, never in this bundle:

```console
sweforge secret set owner/repo RELEASE_CATALOG_TOKEN
sweforge secret set owner/repo RELEASE_SERVICE_AUTH
sweforge secret check owner/repo
```

`secret_env` and `secret_headers` hold *references*. The reference names are
part of the configuration digest; the values are not. Rotating a value
therefore takes effect on the next fresh MCP client without creating a new
generation, and existing IssueThreads keep the reference set they were bound
to while resolving today's value for it.

Resolution is repository-scoped and fails closed: a missing secret raises
before the client is constructed, so no connection is attempted. Repository A
never resolves repository B's value for the same secret name.

Credential values never reach the model, the tool schemas, the system prompt,
checkpoints, traces, logs, or GitHub comments. Transport errors are redacted
before they propagate, so a server that echoes `Authorization` back in an
error message cannot leak it. `sweforge repo show` reports how many
credentials a server requires and how many are configured - never a name it
would be unsafe to print, and never a value.

## Transport safety

Remote URLs are trusted operator configuration and are never model-supplied.
TLS verification stays at library defaults. A server declaring
`secret_headers` must use `https://`, and its client is constructed with
redirects disabled so a redirect cannot forward credentials to another host.
