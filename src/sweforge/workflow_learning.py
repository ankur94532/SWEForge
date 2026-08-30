"""Post-publication learning for declarative workflow lifecycles."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .agent_trace import AgentTracer, TraceContext
from .github_store import (
    IssueResolutionStatus,
    RepoMemoryCandidateStatus,
    SQLiteGitHubStore,
)
from .issue_resolution import (
    ResolutionEvidence,
    bounded_changed_files,
    curate_issue_resolution,
)
from .memory_learning import (
    MemoryLearningResult,
    MemoryLearningStatus,
    RepoMemoryCandidate,
    apply_memory_candidates,
    candidate_from_proposal,
    curate_repository_memory,
)
from .repo_memory import read_repo_memory, repo_memory_namespace
from .workspace import Workspace

UNCONFIGURED_MEMORY_LEARNING = "no repository memory curator was configured"
UNCONFIGURED_ISSUE_RESOLUTION = "no issue resolution curator was configured"


class WorkflowLearningService:
    """Drain bounded learning jobs created by publication finalization."""

    def __init__(
        self,
        *,
        store: SQLiteGitHubStore,
        memory_store: Any,
        memory_model: str | None,
        resolution_model: str | None,
        lock_root: str | Path,
        clock,
        tracer: AgentTracer | None = None,
    ) -> None:
        self.store = store
        self.memory_store = memory_store
        self.memory_model = memory_model
        self.resolution_model = resolution_model
        self.lock_root = lock_root
        self.clock = clock
        self.tracer = tracer

    def _trace_context(
        self, thread_id: str, cycle_id: int, *, role: str, model: str
    ) -> TraceContext:
        try:
            thread = self.store.issue_thread(thread_id)
            cycle = self.store.connection.execute(
                """SELECT workflow_cycle_id FROM workflow_cycles_v1
                   WHERE thread_id=? AND cycle_id=?""",
                (thread_id, cycle_id),
            ).fetchone()
        except Exception:
            return TraceContext(
                thread_id=thread_id,
                cycle_id=cycle_id,
                model_role=role,
                model=model,
            )
        return TraceContext(
            thread_id=thread_id,
            repo=str(thread["repo_full_name"]) if thread is not None else "",
            issue_number=int(thread["issue_number"]) if thread is not None else None,
            workflow_cycle_id=str(cycle["workflow_cycle_id"]) if cycle else "",
            cycle_id=cycle_id,
            model_role=role,
            model=model,
        )

    def _trace_model_call(
        self,
        *,
        thread_id: str,
        cycle_id: int,
        role: str,
        model: str,
        callback,
    ):
        if self.tracer is None:
            return callback()
        context = self._trace_context(thread_id, cycle_id, role=role, model=model)
        self.tracer.emit(
            "MODEL START", f"{context.model_role}_model={context.model}", context
        )
        try:
            result = callback()
        except Exception as exc:
            self.tracer.emit("MODEL ERROR", f"{type(exc).__name__}: {exc}", context)
            self.tracer.emit("MODEL END", "failed", context)
            raise
        self.tracer.emit("MODEL END", "completed", context)
        return result

    def process_one(self, thread_id: str) -> bool:
        learning = self.store.pending_memory_learning(thread_id)
        if learning is not None:
            self._learn_repository(learning)
            return True
        resolution = self.store.pending_issue_resolution(thread_id)
        if resolution is not None:
            self._learn_resolution(resolution)
            return True
        return False

    def _workspace(self, thread_id: str) -> tuple[Workspace | None, str | None]:
        record = self.store.thread_workspace(thread_id)
        if record is None:
            return None, None
        path = Path(record.workspace_path)
        return Workspace(path, path, record.base_commit), str(path)

    def _task_material(self, thread_id: str, cycle_id: int) -> tuple[str, str, str]:
        rows = self.store.connection.execute(
            """SELECT t.task_id,p.plan_text,e.summary,v.summary AS validation_summary
               FROM workflow_task_runs_v1 AS t
               JOIN workflow_task_plans_v1 AS p ON p.plan_id=t.current_plan_id
               JOIN workflow_task_executions_v1 AS e
                 ON e.task_run_id=t.task_run_id AND e.attempt=t.execution_attempt
               JOIN workflow_task_validations_v1 AS v
                 ON v.task_run_id=t.task_run_id
                AND v.validation_round=t.validation_round
               WHERE t.thread_id=? AND t.cycle_id=?
               ORDER BY t.declaration_index""",
            (thread_id, cycle_id),
        ).fetchall()
        plans = "\n\n".join(f"{row['task_id']}: {row['plan_text']}" for row in rows)
        executions = "\n\n".join(f"{row['task_id']}: {row['summary']}" for row in rows)
        validations = "\n\n".join(
            f"{row['task_id']}: {row['validation_summary']}" for row in rows
        )
        return plans, executions, validations

    def _learn_repository(self, learning) -> None:
        now = self.clock()
        attempt = self.store.claim_memory_learning_attempt(learning.learning_id)
        learning = replace(learning, attempt_count=attempt)
        proposal_json = "[]"
        proposal_records = self.store.repo_memory_candidates_for_cycle(
            thread_id=learning.thread_id,
            cycle_id=learning.cycle_id,
            root_event_key=learning.source_event_key,
            root_input_id=learning.root_input_id,
        )
        try:
            workspace, path = self._workspace(learning.thread_id)
            if self.memory_store is None or workspace is None or path is None:
                result = MemoryLearningResult(MemoryLearningStatus.NO_UPDATE)
                error = "repository memory store or workflow workspace is unavailable"
            else:
                candidates: list[RepoMemoryCandidate] = []
                for record in proposal_records:
                    try:
                        candidates.append(
                            candidate_from_proposal(
                                repo_id=learning.repo_id,
                                worktree=path,
                                category=record.category,
                                fact=record.fact,
                                durability_reason=record.durability_reason,
                                path=record.evidence_path,
                                start_line=record.evidence_start_line,
                                end_line=record.evidence_end_line,
                            )
                        )
                    except (OSError, UnicodeError, ValueError):
                        continue
                if self.memory_model:
                    plans, _, _ = self._task_material(
                        learning.thread_id, learning.cycle_id
                    )
                    curated = self._trace_model_call(
                        thread_id=learning.thread_id,
                        cycle_id=learning.cycle_id,
                        role="memory",
                        model=self.memory_model,
                        callback=lambda: curate_repository_memory(
                            model=self.memory_model,
                            repo_id=learning.repo_id,
                            worktree=path,
                            changed_files=workspace.changed_files()[:100],
                            diff=workspace.diff()[:40_000],
                            existing_memory=read_repo_memory(
                                self.memory_store,
                                repo_memory_namespace(learning.repo_id),
                            )
                            or "",
                            plan_text=plans[:12_000],
                        ),
                    )
                    proposal_json = curated.proposal_json
                    candidates.extend(curated.candidates)
                result = apply_memory_candidates(
                    self.memory_store,
                    repo_id=learning.repo_id,
                    worktree=path,
                    candidates=candidates,
                    lock_root=self.lock_root,
                )
                error = result.error or (
                    UNCONFIGURED_MEMORY_LEARNING if not self.memory_model else None
                )
        except Exception as exc:
            result = MemoryLearningResult(
                MemoryLearningStatus.FAILED, error=str(exc)[:500]
            )
            error = result.error
        self._settle_memory_proposals(proposal_records, result, now=now)
        self.store.save_repo_memory_learning(
            replace(
                learning,
                status=result.status.value,
                accepted_candidates=result.accepted_candidates,
                rejected_candidates=result.rejected_candidates,
                proposal_json=proposal_json,
                error_message=error,
                updated_at=now,
            )
        )

    def _settle_memory_proposals(self, records, result, *, now: str) -> None:
        accepted = result.status is MemoryLearningStatus.UPDATED
        for record in records:
            if record.status != RepoMemoryCandidateStatus.PROPOSED.value:
                continue
            self.store.set_repo_memory_candidate_status(
                record.candidate_id,
                status=(
                    RepoMemoryCandidateStatus.ACCEPTED.value
                    if accepted
                    else RepoMemoryCandidateStatus.REJECTED.value
                ),
                now=now,
            )

    def _learn_resolution(self, resolution) -> None:
        now = self.clock()
        attempt = self.store.claim_issue_resolution_attempt(resolution.resolution_id)
        resolution = replace(resolution, attempt_count=attempt, updated_at=now)
        if not self.resolution_model:
            self.store.save_issue_resolution(
                replace(
                    resolution,
                    status=IssueResolutionStatus.NO_CASE.value,
                    error_message=UNCONFIGURED_ISSUE_RESOLUTION,
                )
            )
            return
        try:
            workspace, _ = self._workspace(resolution.thread_id)
            plans, executions, validations = self._task_material(
                resolution.thread_id, resolution.cycle_id
            )
            publication = self.store.publication_for_id(resolution.publication_id or "")
            source = self.store.source_event(resolution.source_event_key)
            changed = tuple(workspace.changed_files()[:60]) if workspace else ()
            diff = workspace.diff()[:20_000] if workspace else ""
            evidence = ResolutionEvidence(
                issue_number=resolution.issue_number,
                issue_title=resolution.issue_title,
                issue_description=resolution.issue_description_snapshot,
                task_text=(source["body"] if source else "") or "",
                plan_text=plans,
                execution_response=executions,
                changed_files=changed,
                diff=diff,
                review_summary=validations,
                repair_rounds=self._repair_rounds(
                    resolution.thread_id, resolution.cycle_id
                ),
                publication_status=publication.status.value if publication else "",
                pr_number=publication.pr_number if publication else None,
                pr_url=publication.pr_url if publication else None,
                commit_sha=publication.remote_commit_sha if publication else None,
            )
            case = self._trace_model_call(
                thread_id=resolution.thread_id,
                cycle_id=resolution.cycle_id,
                role="resolution",
                model=self.resolution_model,
                callback=lambda: curate_issue_resolution(
                    model=self.resolution_model, evidence=evidence
                ),
            )
        except Exception as exc:
            self.store.save_issue_resolution(
                replace(
                    resolution,
                    status=IssueResolutionStatus.FAILED.value,
                    error_message=str(exc)[:500],
                )
            )
            return
        if not case.useful:
            self.store.save_issue_resolution(
                replace(
                    resolution,
                    status=IssueResolutionStatus.NO_CASE.value,
                    error_message=None,
                )
            )
            return
        self.store.save_issue_resolution(
            replace(
                resolution,
                task_summary=case.task_summary,
                symptom_summary=case.symptom_summary,
                root_cause=case.root_cause,
                fix_summary=case.fix_summary,
                affected_components_json=json.dumps(case.affected_components),
                changed_files_json=bounded_changed_files(changed),
                validation_summary=case.validation_summary,
                search_terms_json=json.dumps(case.search_terms),
                limitations=case.limitations,
                commit_sha=publication.remote_commit_sha if publication else None,
                pr_number=publication.pr_number if publication else None,
                pr_url=publication.pr_url if publication else None,
                status=IssueResolutionStatus.COMPLETED.value,
                error_message=None,
            )
        )

    def _repair_rounds(self, thread_id: str, cycle_id: int) -> int:
        row = self.store.connection.execute(
            """SELECT COALESCE(SUM(validation_round - 1),0) AS rounds
               FROM workflow_task_runs_v1 WHERE thread_id=? AND cycle_id=?""",
            (thread_id, cycle_id),
        ).fetchone()
        return int(row["rounds"])
