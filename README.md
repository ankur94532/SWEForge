# SWEForge

SWEForge is a small local walking skeleton for software-engineering agents.
It creates a temporary Git worktree, runs a Deep Agent against that worktree,
and reports the agent's response and resulting diff.

## Setup

```bash
uv sync
export SWEFORGE_MODEL=anthropic:claude-sonnet-4-6
export ANTHROPIC_API_KEY=...
```

Other provider model strings supported by LangChain can be used, for example
`openai:gpt-5.5` or `google_genai:gemini-3.6-flash`, with the corresponding
provider package and credentials installed.

## Run

```bash
uv run sweforge /path/to/git/repository "Fix the failing tests"
```

Use `--keep-worktree` to retain the temporary worktree for inspection. Without
it, SWEForge removes the temporary worktree after collecting the result.

## Safety boundary

The worktree isolates changes from the primary checkout, but it is not a
security sandbox. Deep Agents' `LocalShellBackend` executes commands directly
on the host with the process user's permissions. Do not use this V0 CLI with
untrusted tasks or repositories.
