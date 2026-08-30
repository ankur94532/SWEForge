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
