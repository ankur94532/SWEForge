# SWEForge

SWEForge V0 is a small local walking skeleton for software-engineering agents.
Its flow is: task → temporary Git worktree → Deep Agent → inspect/edit/test →
diff/result.

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

The temporary worktree is preserved by default and its path is printed for
human inspection. Use `--discard-worktree` to explicitly remove it after the
run. Failed runs also retain their worktree for debugging.

## Safety boundary

The worktree isolates changes from the primary checkout, but it is not a
security sandbox. Deep Agents' `LocalShellBackend` executes commands directly
on the host with the process user's permissions. Do not use this V0 CLI with
untrusted tasks or repositories.

## Conceptual boundaries

- TOOLS are what the agent can do.
- SKILLS are specialized procedural knowledge.
- MEMORY is previously learned repository knowledge.
- MODEL is the reasoning engine.
- DEEP AGENTS is the inner agent harness.
- LANGGRAPH is the durable orchestration/runtime.
- SWEFORGE owns the SWE-specific lifecycle and composition.

Future work may add GitHub polling, durable threads, per-thread
sandboxes/workspaces, repository-scoped memory/skills/tools, and
multi-repository execution. Those are planned boundaries, not V0 features.
