"""Canonical Deep Agent driver used by the production declarative controller."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from deepagents.middleware.permissions import FilesystemPermission
from langchain_core.tools import tool

from .agent import (
    _build_backend,
    build_durable_workflow_agent,
    pending_interrupt_values,
)
from .capabilities import RepoCapabilityRegistry, load_repo_mcp_tools
from .context import RepoAgentContext
from .execution_security import SandboxBackendProvider, require_secure_backend
from .github_models import InteractionMode
from .github_store import (
    RepoMemoryCandidateRecord,
    RepoMemoryCandidateStatus,
    SQLiteGitHubStore,
    repo_memory_candidate_id_for,
)
from .memory_learning import candidate_from_proposal
from .repo_memory import MEMORY_VIRTUAL_PATH, ensure_repo_memory, repo_memory_namespace
from .skills import show_repo_skill
from .workflow_agent_runtime import invoke_workflow_phase
from .workflow_middleware import WorkflowAuthority
from .workflow_runtime import TaskRun, WorkflowCycle, WorkflowRuntime
from .workflow_spec import DEFAULT_WORKFLOW, WorkflowSpec
from .workflow_tools import build_lifecycle_tools


class DeepAgentWorkflowDriver:
    """Reconstruct one logical root graph and resume its IssueThread checkpoint."""

    def __init__(
        self,
        *,
        runtime: WorkflowRuntime,
        workflow_cycle_id: str,
        spec: WorkflowSpec,
        store: SQLiteGitHubStore,
        client: Any,
        worktree: Path,
        planning_model: str,
        execution_model: str,
        validation_model: str,
        checkpointer: object,
        memory_store: Any,
        capability_registry: RepoCapabilityRegistry | None,
        sandbox_backend_provider: SandboxBackendProvider | None,
        secure_execution: bool,
        unsafe_local_shell: bool,
    ) -> None:
        self.runtime = runtime
        self.workflow_cycle_id = workflow_cycle_id
        self.spec = spec
        self.store = store
        self.client = client
        self.worktree = worktree
        self.planning_model = planning_model
        self.execution_model = execution_model
        self.validation_model = validation_model
        self.checkpointer = checkpointer
        self.memory_store = memory_store
        self.capability_registry = capability_registry
        self.sandbox_backend_provider = sandbox_backend_provider
        self.secure_execution = secure_execution
        self.unsafe_local_shell = unsafe_local_shell

    def drive(
        self,
        *,
        cycle: WorkflowCycle,
        task: TaskRun,
        prompt: str,
        resume: dict[str, Any] | None = None,
    ) -> None:
        self._assert_cycle_spec(cycle)
        agent, authority, context = self._agent(cycle)
        invoke_workflow_phase(
            agent,
            authority=authority,
            thread_id=cycle.thread_id,
            prompt=prompt,
            context=context,
            resume=resume,
        )
        fresh = self.runtime.active_task(cycle.workflow_cycle_id)
        if fresh is not None:
            self.reconcile_interrupts(cycle=cycle, task=fresh)

    def reconcile_interrupts(self, *, cycle: WorkflowCycle, task: TaskRun) -> None:
        self._assert_cycle_spec(cycle)
        agent, _, _ = self._agent(cycle)
        config = {"configurable": {"thread_id": cycle.thread_id}}
        for payload in pending_interrupt_values(agent, config):
            if payload.get("task_run_id") != task.task_run_id:
                continue
            if payload.get("kind") == "CLARIFICATION":
                self._publish_clarification(cycle, payload)

    def has_pending_interrupt(
        self,
        *,
        cycle: WorkflowCycle,
        task: TaskRun,
        kind: str,
        occurrence_key: str,
    ) -> bool:
        self._assert_cycle_spec(cycle)
        agent, _, _ = self._agent(cycle)
        config = {"configurable": {"thread_id": cycle.thread_id}}
        matches = [
            payload
            for payload in pending_interrupt_values(agent, config)
            if payload.get("task_run_id") == task.task_run_id
            and payload.get("kind") == kind
            and payload.get("occurrence_key") == occurrence_key
        ]
        return len(matches) == 1

    def _assert_cycle_spec(self, cycle: WorkflowCycle) -> None:
        if (
            cycle.workflow_cycle_id != self.workflow_cycle_id
            or cycle.workflow_id != self.spec.workflow_id
            or cycle.workflow_version != self.spec.version
            or cycle.workflow_digest != self.spec.digest
        ):
            raise PermissionError("workflow driver specification is not cycle-bound")

    def _agent(self, cycle: WorkflowCycle):
        root = self._root_event(cycle)
        context = RepoAgentContext(
            repo_id=root["repo_id"],
            repo_full_name=root["repo_full_name"],
            thread_id=cycle.thread_id,
        )
        if self.secure_execution:
            isolated = require_secure_backend(
                context=context,
                worktree=str(self.worktree),
                provider=self.sandbox_backend_provider,
                unsafe_local_shell=self.unsafe_local_shell,
            )
        else:
            isolated = None
        ensure_repo_memory(self.memory_store, repo_memory_namespace(context.repo_id))
        observations: list[dict[str, Any]] = []

        def capture_execution_evidence(**observation: Any) -> None:
            observations.append(dict(observation))

        validations: list[dict[str, Any]] = []

        backend = _build_backend(
            str(self.worktree),
            memory_store=self.memory_store,
            repo_context=context,
            skills_store=self.memory_store,
            sandbox_backend=isolated,
            execution_evidence_sink=capture_execution_evidence,
        )
        authority = WorkflowAuthority(self.runtime, cycle.workflow_cycle_id, self.spec)
        lifecycle = build_lifecycle_tools(
            runtime=self.runtime,
            workflow_cycle_id=cycle.workflow_cycle_id,
            publish_plan=lambda **kwargs: self._publish_plan(cycle, **kwargs),
            publish_result=lambda **kwargs: self._publish_result(cycle, **kwargs),
            execution_evidence=lambda: list(observations),
            validation_evidence=lambda: list(validations),
        )
        extra = [
            self._validation_tool(cycle, validations),
            self._issue_memory_tool(context.repo_id),
            self._repo_memory_proposal_tool(cycle, context.repo_id),
        ]
        if self.capability_registry is not None:
            mcp_tools, _ = asyncio.run(
                load_repo_mcp_tools(self.capability_registry, context)
            )
            extra.extend(mcp_tools)
        permissions = [
            FilesystemPermission(
                operations=["write"], paths=["/memories/**"], mode="deny"
            ),
            FilesystemPermission(
                operations=["write"], paths=["/skills/**"], mode="deny"
            ),
        ]
        agent = build_durable_workflow_agent(
            planning_model=self.planning_model,
            execution_model=self.execution_model,
            validation_model=self.validation_model,
            backend=backend,
            authority=authority,
            lifecycle_tools=lifecycle,
            capability_tools=extra,
            read_skill=lambda _cycle_id, skill: self._read_skill(
                context.repo_id, skill
            ),
            checkpointer=self.checkpointer,
            store=self.memory_store,
            context_schema=RepoAgentContext,
            memory=[MEMORY_VIRTUAL_PATH],
            permissions=permissions,
        )
        return agent, authority, context

    def _publish_plan(
        self,
        cycle: WorkflowCycle,
        *,
        task_run_id: str,
        task_id: str,
        plan_text: str,
    ) -> tuple[int, str]:
        root = self._root_event(cycle)
        row = self.store.connection.execute(
            """SELECT COALESCE(MAX(version),0)+1 FROM workflow_task_plans_v1
               WHERE task_run_id=?""",
            (task_run_id,),
        ).fetchone()
        version = int(row[0])
        digest = hashlib.sha256(plan_text.strip().encode()).hexdigest()
        marker = f"<!-- sweforge:task-plan:{task_run_id}:v{version}:{digest} -->"
        repo = self.client.repository(root["repo_full_name"])
        comments = self._conversation_comments(repo, root)
        matches = [item for item in comments if marker in (item.get("body") or "")]
        if len(matches) > 1:
            raise RuntimeError("multiple matching task plan comments are ambiguous")
        body = (
            f"{marker}\n### SWEForge task `{task_id}` plan — v{version}\n\n"
            f"{plan_text.strip()}\n\n"
        )
        if self.store.interaction_mode(cycle.thread_id) == InteractionMode.AUTO:
            body += (
                "AUTO mode is enabled. This exact plan has been recorded and "
                "SWEForge will proceed automatically."
            )
        else:
            body += (
                "Reply with `@agent approve` to execute this exact plan, or "
                "`@agent <feedback>` to revise it."
            )
        comment = matches[0] if matches else self._post_response(repo, root, body)
        return int(comment["id"]), str(
            comment.get("created_at") or self.runtime.clock()
        )

    def _publish_result(
        self,
        cycle: WorkflowCycle,
        *,
        task_run_id: str,
        task_id: str,
    ) -> tuple[int, str]:
        task = self.runtime.task(task_run_id)
        execution = self.store.connection.execute(
            """SELECT * FROM workflow_task_executions_v1
               WHERE task_run_id=? AND plan_id=? AND attempt=?""",
            (task_run_id, task.current_plan_id, task.execution_attempt),
        ).fetchone()
        validation = self.store.connection.execute(
            """SELECT * FROM workflow_task_validations_v1
               WHERE task_run_id=? AND plan_id=? AND execution_attempt=?
               ORDER BY validation_round DESC LIMIT 1""",
            (task_run_id, task.current_plan_id, task.execution_attempt),
        ).fetchone()
        if execution is None or validation is None or validation["verdict"] != "ACCEPT":
            raise RuntimeError("exact accepted validation is missing")
        identity = hashlib.sha256(
            (
                f"{task_run_id}\0{task.current_plan_id}\0"
                f"{execution['execution_id']}\0{validation['validation_id']}"
            ).encode()
        ).hexdigest()[:24]
        marker = f"<!-- sweforge:task-result:{identity} -->"
        root = self._root_event(cycle)
        repo = self.client.repository(root["repo_full_name"])
        comments = self._conversation_comments(repo, root)
        matches = [item for item in comments if marker in (item.get("body") or "")]
        if len(matches) > 1:
            raise RuntimeError("multiple matching task result comments are ambiguous")
        body = (
            f"{marker}\n### SWEForge task `{task_id}` implementation ready for review"
            f"\n\nThe approved plan has been executed and validation passed.\n\n"
            f"Execution:\n- {execution['summary'][:2_000]}\n\n"
            f"Validation:\n- {validation['summary'][:2_000]}\n- Verdict: ACCEPT\n\n"
        )
        if self.store.interaction_mode(cycle.thread_id) == InteractionMode.AUTO:
            body += (
                "AUTO mode is enabled; SWEForge will accept this exact result "
                "automatically."
            )
        else:
            body += (
                "Reply with `@agent approve` to accept this task result, or "
                "`@agent <feedback>` to request changes."
            )
        comment = matches[0] if matches else self._post_response(repo, root, body)
        return int(comment["id"]), str(
            comment.get("created_at") or self.runtime.clock()
        )

    def _publish_clarification(
        self, cycle: WorkflowCycle, payload: dict[str, Any]
    ) -> None:
        root = self._root_event(cycle)
        occurrence = str(payload["occurrence_key"])
        marker = f"<!-- sweforge:clarification:{occurrence} -->"
        repo = self.client.repository(root["repo_full_name"])
        comments = self._conversation_comments(repo, root)
        matches = [item for item in comments if marker in (item.get("body") or "")]
        if len(matches) > 1:
            raise RuntimeError("multiple clarification comments are ambiguous")
        if not matches:
            self._post_response(
                repo,
                root,
                f"{marker}\n### SWEForge needs input\n\n{payload['question']}\n\n"
                f"Reason: {payload['reason']}\n\nReply with `@agent <answer>`.",
            )

    def _conversation_comments(self, repo: Any, root: Any) -> list[dict[str, Any]]:
        if root["origin_surface"] == "PR_INLINE_REVIEW":
            return self.client.review_comments(repo, root["subject_number"])
        return self.client.comments(repo, root["subject_number"])

    def _post_response(self, repo: Any, root: Any, body: str) -> dict[str, Any]:
        if root["origin_surface"] == "PR_INLINE_REVIEW":
            reply_to = root["review_thread_root_id"] or root["source_id"]
            if not reply_to:
                raise RuntimeError("inline review response target is missing")
            return self.client.create_review_comment_reply(
                repo, root["subject_number"], int(reply_to), body
            )
        return self.client.create_comment(repo, root["subject_number"], body)

    def _root_event(self, cycle: WorkflowCycle):
        root = self.store.source_event(cycle.root_input_id)
        if root is not None:
            return root
        deferred = self.store.deferred_followup_by_id(cycle.root_input_id)
        if deferred is None:
            raise RuntimeError("workflow root input disappeared")
        root = self.store.source_event(deferred["event_key"])
        if root is None:
            raise RuntimeError("workflow root SourceEvent disappeared")
        return root

    def _read_skill(self, repo_id: int, skill: str) -> str:
        content = show_repo_skill(self.memory_store, repo_id, f"{skill}/SKILL.md")
        if content:
            return content
        if self.spec.digest == DEFAULT_WORKFLOW.digest:
            return (
                "Inspect the repository carefully, follow the approved scope, use "
                "the current phase tools, and provide concrete validation evidence."
            )
        raise PermissionError(f"required operator skill is missing: {skill}")

    def _validation_tool(self, cycle: WorkflowCycle, validations: list[dict[str, Any]]):
        @tool
        def run_validation() -> str:
            """Return deterministic cumulative diff and execution evidence."""
            workspace = self.store.thread_workspace(cycle.thread_id)
            if workspace is None:
                raise RuntimeError("workflow workspace is missing")
            path = Path(workspace.workspace_path)
            import subprocess

            diff = subprocess.run(
                ["git", "diff", "--no-ext-diff", workspace.base_commit, "--"],
                cwd=path,
                capture_output=True,
                text=True,
                check=True,
            ).stdout[:60_000]
            executions = self.store.connection.execute(
                """SELECT task_run_id,attempt,summary,evidence_json
                   FROM workflow_task_executions_v1 WHERE workflow_cycle_id=?
                   ORDER BY task_run_id,attempt""",
                (cycle.workflow_cycle_id,),
            ).fetchall()
            payload = {
                "base_commit": workspace.base_commit,
                "diff": diff,
                "executions": [dict(row) for row in executions],
            }
            validations.append(payload)
            return json.dumps(payload, sort_keys=True)

        return run_validation

    def _issue_memory_tool(self, repo_id: int):
        @tool
        def search_issue_memory(query: str, limit: int = 3) -> str:
            """Search resolved issues from this authoritative repository only."""
            rows = self.store.search_issue_resolutions(
                repo_id=repo_id, query=query, limit=limit, per_thread_limit=1
            )
            return (
                "\n\n".join(
                    f"Issue #{row.issue_number}: {row.task_summary}\n"
                    f"Root cause: {row.root_cause}\nFix: {row.fix_summary}"
                    for row in rows
                )
                or "No relevant resolved issues found."
            )

        return search_issue_memory

    def _repo_memory_proposal_tool(self, cycle: WorkflowCycle, repo_id: int):
        @tool
        def propose_repo_memory(
            category: str,
            fact: str,
            durability_reason: str,
            path: str,
            start_line: int,
            end_line: int,
        ) -> str:
            """Nominate repository lines for application-validated learning."""
            candidate_from_proposal(
                repo_id=repo_id,
                worktree=self.worktree,
                category=category,
                fact=fact,
                durability_reason=durability_reason,
                path=path,
                start_line=start_line,
                end_line=end_line,
            )
            now = self.runtime.clock()
            self.store.save_repo_memory_candidate(
                RepoMemoryCandidateRecord(
                    candidate_id=repo_memory_candidate_id_for(
                        repo_id=repo_id,
                        thread_id=cycle.thread_id,
                        cycle_id=cycle.cycle_id,
                        root_input_id=cycle.root_input_id,
                        fact=fact,
                        evidence_path=path,
                        evidence_start_line=start_line,
                        evidence_end_line=end_line,
                    ),
                    repo_id=repo_id,
                    thread_id=cycle.thread_id,
                    cycle_id=cycle.cycle_id,
                    root_input_id=cycle.root_input_id,
                    source_event_key=self._root_event(cycle)["event_key"],
                    category=category,
                    fact=fact,
                    durability_reason=durability_reason,
                    evidence_path=path,
                    evidence_start_line=start_line,
                    evidence_end_line=end_line,
                    status=RepoMemoryCandidateStatus.PROPOSED.value,
                    created_at=now,
                    updated_at=now,
                )
            )
            return "Recorded for application validation after cumulative publication."

        return propose_repo_memory
