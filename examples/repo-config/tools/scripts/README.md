# Registered script tools

Each subdirectory is one registered tool: a `tool.yaml` describing it and the
fixed entrypoint it names. The directory name is not the tool name - the model
sees the `name` field, and `sweforge tool remove` resolves that name back to
its directory.

## Schema

```yaml
version: 1
name: validate_release          # model-facing tool name
description: ...                # <= 300 characters
runtime: python | shell
entrypoint: validate_release.py # relative, inside this directory
effect: read | mutate
timeout_seconds: 30             # 1..600
env:                            # fixed non-secret environment
  RELEASE_REGION: example-region
secret_env:                     # env name -> repository secret name
  RELEASE_POLICY_TOKEN: RELEASE_POLICY_TOKEN
args_schema:                    # object schema; additionalProperties: false
  type: object
  properties: {config_path: {type: string}}
  required: [config_path]
  additionalProperties: false
```

## What the model can and cannot do

The model sees the name, the description, and `args_schema` - nothing else. It
cannot choose the entrypoint, the runtime, the timeout, the environment, or a
credential, and arguments outside the schema are rejected before the process
starts. Passing an argument named after a secret does not inject it.

`effect` is structural rather than advisory: a `mutate` tool named by a
planning or validation phase fails bundle validation, so a read-only phase
cannot be handed a writing capability by mistake.

## Execution contract

The script is staged from the bound configuration generation, runs in the
current worktree, and receives:

- JSON arguments on standard input, as one object
- a minimal environment: `PATH`, `LANG`, the fixed `env`, and resolved
  `secret_env` values

Standard output is returned to the model, bounded and with any exact injected
secret value replaced by `[REDACTED]`. A non-zero exit adds the exit code and
bounded stderr. Exceeding `timeout_seconds` fails the call. Nothing else about
the process is exposed.

Credentials are resolved per invocation from the encrypted repository secret
store, so rotation applies to the next call without changing the bound
generation, and a deleted value makes the tool fail closed. The generic
`execute` built-in never inherits any of these values.
