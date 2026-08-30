# Adding a repository to SWEForge

Repository authority comes from an operator-installed bundle, never from files
inside the target checkout.

1. Create a starter bundle:

   ```console
   sweforge repo init owner/repo
   ```

2. Edit `workflow.yaml` using the declarative task/phase schema.
3. Add each referenced skill at `skills/<name>/SKILL.md`, including `name` and
   a bounded `description` in YAML frontmatter.
4. Optionally add registered Python or shell tools under
   `tools/scripts/<tool>/`. Registered tools receive JSON arguments on standard
   input, have a fixed entrypoint and timeout, and declare `effect: read` or
   `effect: mutate`.
5. Optionally add trusted MCP definitions in `tools/mcp/servers.yaml`.
6. Validate the candidate:

   ```console
   sweforge repo validate owner/repo ./owner-repo-sweforge
   ```

7. Install it atomically:

   ```console
   sweforge repo configure owner/repo ./owner-repo-sweforge
   ```

8. Inspect safe metadata:

   ```console
   sweforge repo show owner/repo
   ```

9. Create a GitHub issue invoking `@agent`.

The new IssueThread binds the repository's current immutable configuration
generation when it is ingested. Later configuration updates affect new issues
only. Existing issues, restarts, revisions, skills, registered scripts, and MCP
allowlists continue using the originally bound generation. Repository memory
remains repository-scoped but is intentionally not generation-versioned.

Unconfigured repositories retain the existing server-selected workflow and
trusted built-ins for backward compatibility. Pre-existing threads without a
generation binding keep that legacy compatibility path; SWEForge never guesses
or retroactively invents an installed generation for them.

## Repository credentials

Generate an operator master key once and provide it to SWEForge through
`SWEFORGE_SECRET_MASTER_KEY` or a `0600` file named by
`SWEFORGE_SECRET_MASTER_KEY_FILE`. Values are encrypted at rest with Fernet.

Configure a value with a hidden prompt (or use `--stdin` for controlled
automation):

```console
sweforge secret set owner/repo DEPLOY_API_TOKEN
sweforge secret list owner/repo
sweforge secret check owner/repo
```

Registered script tools reference credentials without containing values:

```yaml
env:
  DEPLOY_REGION: us-east-1
secret_env:
  DEPLOY_API_TOKEN: DEPLOY_API_TOKEN
```

The left side is the process environment name; the right side is the
repository-scoped secret key. Only the registered tool receives those declared
values in its minimal process environment. They are not placed in prompts,
skills, checkpoints, memory, worktrees, generic `execute`, traces, or tool
schemas. Output and errors are redacted if a trusted tool prints an injected
value.

Configuration generations freeze credential reference names, not values.
Rotating a secret immediately affects later calls from existing issues without
changing their bound generation. Deleting a required value makes the tool fail
closed until the value is configured again. Local stdio MCP servers support the
same `secret_env` mapping; remote MCP auth references are not yet supported.
