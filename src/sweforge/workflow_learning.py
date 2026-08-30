"""Post-publication learning for declarative workflow lifecycles."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .agent_trace import AgentTracer, TraceContext
from .execution import normalize_task
from .github_models import format_source_context
from .github_store import (
    IssueResolutionStatus,
    PublicationGeneration,
    PublicationRecord,
    RepoMemoryCandidateStatus,
    SQLiteGitHubStore,
)
from .issue_resolution import (
    ResolutionEvidence,
    bounded_changed_files,
    curate_issue_resolution,
)
from .lifecycle_context import lifecycle_columns, render_accepted_lifecycle
from .memory_learning import (
    MemoryLearningResult,
    MemoryLearningStatus,
    RepoMemoryCandidate,
    apply_memory_candidates,
    candidate_from_proposal,
    curate_repository_memory,
    validate_candidate,
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

    def _workspace(
        self, thread_id: str, *, base_commit: str | None = None
    ) -> tuple[Workspace | None, str | None]:
        record = self.store.thread_workspace(thread_id)
        if record is None:
            return None, None
        path = Path(record.workspace_path)
        return Workspace(path, path, base_commit or record.base_commit), str(path)

    def _task_material(self, thread_id: str, cycle_id: int) -> tuple[str, str, str]:
        """Compatibility material for pre-declarative lifecycle rows."""
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

    def _publication_generation(
        self, *, thread_id: str, cycle_id: int, root_input_id: str
    ) -> tuple[PublicationGeneration | None, PublicationRecord | None]:
        publication = self.store.publication_for_cycle(
            thread_id=thread_id,
            cycle_id=cycle_id,
            root_event_key=root_input_id,
            root_input_id=root_input_id,
        )
        if publication is None:
            return None, None
        try:
            generation = self.store.publication_generation(publication.publication_id)
            return generation, publication
        except ValueError:
            # Legacy publications have no declarative cycle history. Their
            # existing cycle-local compatibility learning remains supported.
            has_declarative = self.store.connection.execute(
                "SELECT 1 FROM workflow_cycles_v1 WHERE thread_id=? LIMIT 1",
                (thread_id,),
            ).fetchone()
            if has_declarative is not None:
                raise
            return None, publication

    def _generation_material(
        self,
        *,
        thread_id: str,
        cycle_id: int,
        root_input_id: str,
    ) -> tuple[
        PublicationGeneration | None,
        PublicationRecord | None,
        str,
        str,
        str,
        str,
        int,
    ]:
        generation, publication = self._publication_generation(
            thread_id=thread_id,
            cycle_id=cycle_id,
            root_input_id=root_input_id,
        )
        if generation is None:
            plans, executions, validations = self._task_material(thread_id, cycle_id)
            count = len([block for block in plans.split("\n\n") if block.strip()])
            return generation, publication, plans, executions, validations, plans, count
        material = self.store.accepted_lifecycle_material(
            thread_id,
            first_cycle_id=generation.first_cycle_id,
            last_cycle_id=generation.last_cycle_id,
        )
        present_cycles = {item.cycle_id for item in material}
        if present_cycles != set(generation.cycle_ids):
            raise ValueError("publication lifecycle material is incomplete")
        plans, executions, validations = lifecycle_columns(material)
        return (
            generation,
            publication,
            plans,
            executions,
            validations,
            render_accepted_lifecycle(material, max_chars=20_000),
            len(material),
        )

    def _generation_workspace(
        self, thread_id: str, generation: PublicationGeneration | None
    ) -> tuple[Workspace | None, str | None]:
        return self._workspace(
            thread_id,
            base_commit=(generation.previous_commit_sha if generation else None),
        )

    def _generation_revision_text(
        self, generation: PublicationGeneration | None
    ) -> tuple[str, int]:
        if generation is None:
            return "", 0
        rendered: list[str] = []
        for item in self.store.revision_inputs_for_generation(generation):
            request = normalize_task(item["residual_text"] or item["source_body"])
            rendered.append(
                f"Revision input {item['revision_input_id']} "
                f"(cycle {item['cycle_id']}):\n"
                + format_source_context(dict(item), request)
            )
        return "\n\n".join(rendered)[:12_000], len(rendered)

    def _learn_repository(self, learning) -> None:
        now = self.clock()
        attempt = self.store.claim_memory_learning_attempt(learning.learning_id)
        learning = replace(learning, attempt_count=attempt)
        proposal_json = "[]"
        proposal_records = []
        proposal_candidate_ids: dict[str, str] = {}
        try:
            material = self._generation_material(
                thread_id=learning.thread_id,
                cycle_id=learning.cycle_id,
                root_input_id=learning.root_input_id,
            )
            (
                generation,
                _publication,
                plans,
                _executions,
                _validations,
                lifecycle_text,
                _lifecycle_records,
            ) = material
            proposal_records = (
                self.store.repo_memory_candidates_for_generation(
                    generation, repo_id=learning.repo_id
                )
                if generation is not None
                else self.store.repo_memory_candidates_for_cycle(
                    thread_id=learning.thread_id,
                    cycle_id=learning.cycle_id,
                    root_event_key=learning.source_event_key,
                    root_input_id=learning.root_input_id,
                )
            )
            if self.tracer is not None and generation is not None:
                context = self._trace_context(
                    learning.thread_id,
                    learning.cycle_id,
                    role="memory",
                    model=self.memory_model or "unconfigured",
                )
                self.tracer.emit(
                    "MEMORY GENERATION START",
                    f"publication={generation.publication_id} "
                    f"cycle_range={generation.first_cycle_id}.."
                    f"{generation.last_cycle_id}",
                    context,
                )
                self.tracer.emit(
                    "MEMORY CANDIDATES", f"proposed={len(proposal_records)}", context
                )
            workspace, path = self._generation_workspace(learning.thread_id, generation)
            if self.memory_store is None or workspace is None or path is None:
                result = MemoryLearningResult(MemoryLearningStatus.NO_UPDATE)
                error = "repository memory store or workflow workspace is unavailable"
            else:
                candidates: list[RepoMemoryCandidate] = []
                for record in proposal_records:
                    try:
                        candidate = candidate_from_proposal(
                            repo_id=learning.repo_id,
                            worktree=path,
                            category=record.category,
                            fact=record.fact,
                            durability_reason=record.durability_reason,
                            path=record.evidence_path,
                            start_line=record.evidence_start_line,
                            end_line=record.evidence_end_line,
                        )
                        validate_candidate(
                            candidate, repo_id=learning.repo_id, worktree=path
                        )
                        candidates.append(candidate)
                        proposal_candidate_ids[record.candidate_id] = (
                            candidate.candidate_id
                        )
                    except (OSError, UnicodeError, ValueError):
                        continue
                if self.memory_model:
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
                            lifecycle_text=lifecycle_text,
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
        self._settle_memory_proposals(
            proposal_records,
            result,
            proposal_candidate_ids=proposal_candidate_ids,
            now=now,
        )
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

    def _settle_memory_proposals(
        self,
        records,
        result,
        *,
        proposal_candidate_ids: dict[str, str],
        now: str,
    ) -> None:
        learning_succeeded = result.status in {
            MemoryLearningStatus.UPDATED,
            MemoryLearningStatus.NO_UPDATE,
        }
        validated = set(result.validated_candidate_ids)
        for record in records:
            if record.status != RepoMemoryCandidateStatus.PROPOSED.value:
                continue
            self.store.set_repo_memory_candidate_status(
                record.candidate_id,
                status=(
                    RepoMemoryCandidateStatus.ACCEPTED.value
                    if learning_succeeded
                    and proposal_candidate_ids.get(record.candidate_id) in validated
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
            generation, publication, plans, executions, validations, _, record_count = (
                self._generation_material(
                    thread_id=resolution.thread_id,
                    cycle_id=resolution.cycle_id,
                    root_input_id=resolution.root_input_id,
                )
            )
            workspace, _ = self._generation_workspace(resolution.thread_id, generation)
            lifecycle = self.store.thread_workflow_lifecycle(resolution.thread_id)
            source = self.store.source_event(
                lifecycle["initial_root_event_key"]
                if lifecycle is not None
                else resolution.source_event_key
            )
            revision_text, revision_count = self._generation_revision_text(generation)
            original_task = normalize_task(source["body"] if source else "")
            task_text = "Original issue request:\n" + original_task[:6_000]
            if revision_text:
                task_text += "\n\nIncorporated steering:\n" + revision_text
            changed = tuple(workspace.changed_files()[:60]) if workspace else ()
            diff = workspace.diff()[:20_000] if workspace else ""
            if self.tracer is not None and generation is not None:
                self.tracer.emit(
                    "ISSUE RESOLUTION GENERATION",
                    f"revision_inputs={revision_count} "
                    f"lifecycle_records={record_count}",
                    self._trace_context(
                        resolution.thread_id,
                        resolution.cycle_id,
                        role="resolution",
                        model=self.resolution_model,
                    ),
                )
            evidence = ResolutionEvidence(
                issue_number=resolution.issue_number,
                issue_title=resolution.issue_title,
                issue_description=resolution.issue_description_snapshot,
                task_text=task_text[:12_000],
                plan_text=plans,
                execution_response=executions,
                changed_files=changed,
                diff=diff,
                review_summary=validations,
                repair_rounds=self._repair_rounds(
                    resolution.thread_id,
                    generation.first_cycle_id if generation else resolution.cycle_id,
                    generation.last_cycle_id if generation else resolution.cycle_id,
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

    def _repair_rounds(
        self, thread_id: str, first_cycle_id: int, last_cycle_id: int
    ) -> int:
        row = self.store.connection.execute(
            """SELECT COALESCE(SUM(validation_round - 1),0) AS rounds
               FROM workflow_task_runs_v1
               WHERE thread_id=? AND cycle_id>=? AND cycle_id<=?""",
            (thread_id, first_cycle_id, last_cycle_id),
        ).fetchone()
        return int(row["rounds"])
