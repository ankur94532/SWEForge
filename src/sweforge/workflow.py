"""Durable planning, approval, and execution gates for IssueThreads."""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from langgraph.store.base import BaseStore

from .execution import ExecutionResult, execute_one, normalize_task
from .github_client import GitHubClient
from .github_models import format_source_context, starts_with_agent_invocation
from .github_store import (
    ExecutionPermit,
    InputPurpose,
    PermitSource,
    PlanRecord,
    PlanStatus,
    SQLiteGitHubStore,
    ThreadWorkspaceRecord,
    WorkflowMode,
    WorkflowPhase,
    WorkflowStateRecord,
)
from .planner import PlannerContext, generate_plan
from .repo_memory import repo_memory_namespace
from .workspace import ThreadWorkspace, WorkspaceError

_PREFIX_RE = re.compile(r"^\s*@agent\b", re.IGNORECASE)
_APPROVE_RE = re.compile(r"^\s*@agent\s+approve\s*$", re.IGNORECASE)
MAX_COMMENT_CHARS = 12_000


def is_exact_approval(body: str) -> bool:
    """Only the complete ``@agent approve`` command authorizes a plan."""
    return _APPROVE_RE.fullmatch(body) is not None


def invocation_text(body: str) -> str | None:
    """Return text after a leading invocation, or ``None`` for non-comments."""
    if not _PREFIX_RE.match(body):
        return None
    return _PREFIX_RE.sub("", body, count=1).strip()


def issue_has_auto_label(issue_payload: dict) -> bool:
    return any(
        isinstance(label, dict) and str(label.get("name", "")).casefold() == "auto"
        for label in issue_payload.get("labels", [])
    )


def _stable_id(*parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return hashlib.sha256(material.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class WorkflowAdvanceResult:
    phase: WorkflowPhase
    thread_id: str
    plan_id: str | None = None
    permit_id: str | None = None
    execution: ExecutionResult | None = None
    message: str = ""


class WorkflowEngine:
    """Application-owned workflow state machine.

    The planner and executor are injected at the boundary, making all tests
    offline. The default planner is the native read-only Deep Agent harness.
    """

    def __init__(
        self,
        *,
        store: SQLiteGitHubStore,
        client: GitHubClient | None = None,
        planner: Callable[..., str] | None = None,
        clock: Callable[[], str] = _now,
    ) -> None:
        self.store = store
        self.client = client
        self.planner = planner or generate_plan
        self.clock = clock

    def start_cycle(
        self,
        *,
        event_key: str,
        plan_text: str,
        mode: WorkflowMode = WorkflowMode.INTERACTIVE,
        posted_comment_id: int | None = None,
    ) -> PlanRecord:
        event = self.store.source_event(event_key)
        if event is None or not event["thread_id"]:
            raise ValueError("workflow root event is missing or unrouted")
        thread_id = event["thread_id"]
        state = self.store.workflow_state(thread_id)
        if state and state.phase != WorkflowPhase.IDLE:
            raise ValueError("IssueThread already has an active workflow")
        cycle_id = state.cycle_id + 1 if state else 1
        version = 1
        plan_id = "plan-" + _stable_id(thread_id, cycle_id, version, plan_text)
        timestamp = self.clock()
        self.store.consume_input(
            event_key,
            thread_id=thread_id,
            cycle_id=cycle_id,
            purpose=InputPurpose.CYCLE_ROOT,
            claimed_at=timestamp,
        )
        record = PlanRecord(
            plan_id=plan_id,
            thread_id=thread_id,
            repo_id=event["repo_id"],
            repo_full_name=event["repo_full_name"],
            issue_number=event["subject_number"],
            cycle_id=cycle_id,
            version=version,
            root_event_key=event_key,
            plan_text=plan_text[:MAX_COMMENT_CHARS],
            status=PlanStatus.POSTED if posted_comment_id else PlanStatus.DRAFT,
            created_at=timestamp,
            posted_at=timestamp if posted_comment_id else None,
            posted_comment_id=posted_comment_id,
            approved_at=None,
            approved_by=None,
            approval_event_key=None,
        )
        self.store.insert_plan(record)
        self.store.save_workflow_state(
            WorkflowStateRecord(
                thread_id=thread_id,
                repo_id=event["repo_id"],
                repo_full_name=event["repo_full_name"],
                issue_number=event["subject_number"],
                phase=(
                    WorkflowPhase.WAITING_FOR_PLAN_APPROVAL
                    if posted_comment_id
                    else WorkflowPhase.PLANNING
                ),
                cycle_id=cycle_id,
                root_event_key=event_key,
                current_plan_id=plan_id,
                mode=mode,
                created_at=state.created_at if state else timestamp,
                updated_at=timestamp,
                response_surface=event["origin_surface"],
                response_subject_number=event["subject_number"],
                response_comment_id=event["source_id"],
                response_url=event["html_url"],
                review_thread_root_id=event["review_thread_root_id"],
            )
        )
        return self.store.plan(plan_id)  # type: ignore[return-value]

    def plan_event(
        self,
        *,
        event_key: str,
        model: str,
        repo_paths: dict[str, str | Path],
        workspace_root: str | Path,
        memory_store: BaseStore | None = None,
        planner: Callable[..., str] | None = None,
    ) -> PlanRecord:
        """Create a plan after proving the planner workspace stayed untouched."""
        event = self.store.source_event(event_key)
        if event is None or not event["thread_id"]:
            raise ValueError("unknown or unrouted event")
        repository = repo_paths.get(event["repo_full_name"])
        if repository is None:
            raise WorkspaceError("no trusted local checkout configured")
        existing = self.store.thread_workspace(event["thread_id"])
        workspace = ThreadWorkspace.create(
            repository=Path(repository).expanduser().resolve(),
            workspace_root=workspace_root,
            repo_id=event["repo_id"],
            issue_number=event["subject_number"],
            existing_path=existing.workspace_path if existing else None,
            expected_branch=existing.branch_name if existing else None,
            expected_base=existing.base_commit if existing else None,
        )
        if not workspace.is_clean():
            raise WorkspaceError("planner workspace was dirty before planning")
        if existing is None:
            timestamp = self.clock()
            self.store.save_thread_workspace(
                ThreadWorkspaceRecord(
                    thread_id=event["thread_id"],
                    repo_id=event["repo_id"],
                    repo_full_name=event["repo_full_name"],
                    issue_number=event["subject_number"],
                    source_repository_path=str(Path(repository).expanduser().resolve()),
                    workspace_path=str(workspace.path),
                    branch_name=workspace.branch_name,
                    base_commit=workspace.base_commit,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        before_head = workspace.head_sha()
        context = PlannerContext(
            worktree=str(workspace.path),
            memory_store=memory_store,
            memory_namespace=repo_memory_namespace(event["repo_id"])
            if memory_store is not None
            else None,
        )
        plan_text = (planner or self.planner)(
            context=context,
            model=model,
            task=format_source_context(event, normalize_task(event["body"])),
        )
        if workspace.head_sha() != before_head or not workspace.is_clean():
            raise WorkspaceError("planner changed the workspace")
        mode = WorkflowMode.INTERACTIVE
        if self.client is not None:
            repo = self.client.repository(event["repo_full_name"])
            if issue_has_auto_label(self.client.issue(repo, event["subject_number"])):
                mode = WorkflowMode.AUTO
        return self.start_cycle(event_key=event_key, plan_text=plan_text, mode=mode)

    def publish_plan(self, plan_id: str) -> PlanRecord:
        plan = self.store.plan(plan_id)
        if plan is None:
            raise ValueError("unknown plan")
        if self.client is None:
            raise ValueError("GitHub client is required to publish a plan")
        repo = self.client.repository(plan.repo_full_name)
        state = self.store.workflow_state(plan.thread_id)
        if state is None:
            raise ValueError("workflow state disappeared")
        marker = f"<!-- sweforge:plan:{plan.plan_id} -->"
        inline = state.response_surface == "PR_INLINE_REVIEW"
        comments = (
            self.client.review_comments_for_pull_request(repo, plan.issue_number)
            if inline
            else self.client.comments(
                repo, state.response_subject_number or plan.issue_number
            )
        )
        matching = [item for item in comments if marker in (item.get("body") or "")]
        if len(matching) > 1:
            raise WorkspaceError("multiple plan comments are ambiguous")
        comment_id = int(matching[0]["id"]) if matching else None
        if comment_id is None:
            mode = state.mode
            suffix = (
                "\n\n`AUTO` is enabled, so SWEForge will continue without waiting "
                "for approval."
                if mode == WorkflowMode.AUTO
                else "\n\nReply with `@agent approve` to execute this exact plan.\n"
                "Or reply with `@agent <feedback>` to revise it."
            )
            body = f"{marker}\n### SWEForge Plan — v{plan.version}"
            body += " · AUTO" if mode == WorkflowMode.AUTO else ""
            body += f"\n\n{plan.plan_text[:MAX_COMMENT_CHARS]}{suffix}"
            if inline:
                reply_to = state.review_thread_root_id or state.response_comment_id
                if not reply_to:
                    raise WorkspaceError("inline review response target is missing")
                comment_id = int(
                    self.client.create_review_comment_reply(repo, int(reply_to), body)[
                        "id"
                    ]
                )
            else:
                comment_id = int(
                    self.client.create_comment(
                        repo, state.response_subject_number or plan.issue_number, body
                    )["id"]
                )
        updated = self.store.update_plan(
            plan.plan_id,
            status=PlanStatus.POSTED,
            posted_at=self.clock(),
            posted_comment_id=comment_id,
        )
        self.store.save_workflow_state(
            WorkflowStateRecord(
                **{
                    **state.__dict__,
                    "phase": WorkflowPhase.WAITING_FOR_PLAN_APPROVAL,
                    "updated_at": self.clock(),
                }
            )
        )
        if self.store.workflow_state(plan.thread_id).mode == WorkflowMode.AUTO:
            self.authorize_auto(thread_id=plan.thread_id)
        return updated

    def approve(
        self, *, event_key: str, author_login: str | None = None
    ) -> ExecutionPermit:
        event = self.store.source_event(event_key)
        if (
            event is None
            or not event["thread_id"]
            or not is_exact_approval(event["body"])
        ):
            raise ValueError("event is not an exact approval")
        state = self.store.workflow_state(event["thread_id"])
        if state is None or state.phase != WorkflowPhase.WAITING_FOR_PLAN_APPROVAL:
            raise ValueError("approval is only valid while waiting for a posted plan")
        if event["origin_surface"] != state.response_surface:
            raise ValueError("approval came from a different conversation surface")
        plan = self.store.current_plan(state.thread_id)
        if plan is None or plan.status != PlanStatus.POSTED:
            raise ValueError("no current posted plan can be approved")
        timestamp = self.clock()
        self.store.consume_input(
            event_key,
            thread_id=state.thread_id,
            cycle_id=state.cycle_id,
            purpose=InputPurpose.PLAN_APPROVAL,
            claimed_at=timestamp,
        )
        self.store.update_plan(
            plan.plan_id,
            status=PlanStatus.APPROVED,
            approved_at=timestamp,
            approved_by=author_login or event["author_login"],
            approval_event_key=event_key,
        )
        permit = ExecutionPermit(
            permit_id="permit-"
            + _stable_id(
                state.thread_id, state.cycle_id, plan.plan_id, "USER", event_key
            ),
            thread_id=state.thread_id,
            cycle_id=state.cycle_id,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            source=PermitSource.USER,
            source_event_key=event_key,
            created_at=timestamp,
            consumed_at=None,
            invalidated_at=None,
        )
        existing = self.store.permit(permit.permit_id)
        if existing:
            return existing
        self.store.insert_permit(permit)
        self.store.save_workflow_state(
            WorkflowStateRecord(
                **{
                    **state.__dict__,
                    "phase": WorkflowPhase.EXECUTION_READY,
                    "updated_at": self.clock(),
                }
            )
        )
        return permit

    def revise(self, *, event_key: str, plan_text: str) -> PlanRecord:
        event = self.store.source_event(event_key)
        if event is None or not event["thread_id"]:
            raise ValueError("unknown workflow input")
        state = self.store.workflow_state(event["thread_id"])
        if state is None or state.phase not in (
            WorkflowPhase.WAITING_FOR_PLAN_APPROVAL,
            WorkflowPhase.EXECUTION_READY,
        ):
            raise ValueError("feedback is not currently accepted for planning")
        if event["origin_surface"] != state.response_surface:
            raise ValueError("feedback came from a different conversation surface")
        current = self.store.current_plan(state.thread_id)
        if current is None:
            raise ValueError("current plan is missing")
        timestamp = self.clock()
        self.store.consume_input(
            event_key,
            thread_id=state.thread_id,
            cycle_id=state.cycle_id,
            purpose=InputPurpose.PLAN_FEEDBACK,
            claimed_at=timestamp,
        )
        self.store.update_plan(current.plan_id, status=PlanStatus.SUPERSEDED)
        version = current.version + 1
        plan_id = "plan-" + _stable_id(
            state.thread_id, state.cycle_id, version, plan_text
        )
        plan = PlanRecord(
            plan_id=plan_id,
            thread_id=state.thread_id,
            repo_id=state.repo_id,
            repo_full_name=state.repo_full_name,
            issue_number=state.issue_number,
            cycle_id=state.cycle_id,
            version=version,
            root_event_key=state.root_event_key,
            plan_text=plan_text[:MAX_COMMENT_CHARS],
            status=PlanStatus.DRAFT,
            created_at=timestamp,
            posted_at=None,
            posted_comment_id=None,
            approved_at=None,
            approved_by=None,
            approval_event_key=None,
        )
        self.store.invalidate_permits(state.thread_id, state.cycle_id, now=timestamp)
        self.store.insert_plan(plan)
        self.store.save_workflow_state(
            WorkflowStateRecord(
                **{
                    **state.__dict__,
                    "phase": WorkflowPhase.PLANNING,
                    "current_plan_id": plan_id,
                    "updated_at": timestamp,
                }
            )
        )
        return plan

    def validate_permit(self, permit_id: str) -> ExecutionPermit:
        permit = self.store.permit(permit_id)
        if permit is None or permit.invalidated_at or permit.consumed_at:
            raise ValueError("execution permit is unavailable")
        state = self.store.workflow_state(permit.thread_id)
        plan = self.store.plan(permit.plan_id)
        if state is None or plan is None:
            raise ValueError("execution permit references missing workflow data")
        if state.phase != WorkflowPhase.EXECUTION_READY:
            raise ValueError("workflow is not ready for execution")
        if state.cycle_id != permit.cycle_id or state.current_plan_id != permit.plan_id:
            raise ValueError("execution permit is stale")
        if plan.version != permit.plan_version or plan.status not in (
            PlanStatus.APPROVED,
            PlanStatus.AUTO_APPROVED,
        ):
            raise ValueError("execution permit does not match current approved plan")
        return permit

    def authorize_auto(self, *, thread_id: str) -> ExecutionPermit:
        state = self.store.workflow_state(thread_id)
        if state is None or state.mode != WorkflowMode.AUTO:
            raise ValueError("AUTO authorization is not enabled")
        if self.client is not None:
            repo = self.client.repository(state.repo_full_name)
            if not issue_has_auto_label(self.client.issue(repo, state.issue_number)):
                self.store.save_workflow_state(
                    WorkflowStateRecord(
                        **{
                            **state.__dict__,
                            "mode": WorkflowMode.INTERACTIVE,
                            "phase": WorkflowPhase.WAITING_FOR_PLAN_APPROVAL,
                            "updated_at": self.clock(),
                        }
                    )
                )
                raise ValueError("AUTO label was removed before authorization")
        plan = self.store.current_plan(thread_id)
        if state.phase != WorkflowPhase.WAITING_FOR_PLAN_APPROVAL or plan is None:
            raise ValueError("AUTO authorization requires a current posted plan")
        timestamp = self.clock()
        self.store.update_plan(
            plan.plan_id, status=PlanStatus.AUTO_APPROVED, approved_at=timestamp
        )
        permit = ExecutionPermit(
            permit_id="permit-"
            + _stable_id(thread_id, state.cycle_id, plan.plan_id, "AUTO"),
            thread_id=thread_id,
            cycle_id=state.cycle_id,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            source=PermitSource.AUTO,
            source_event_key=None,
            created_at=timestamp,
            consumed_at=None,
            invalidated_at=None,
        )
        if not self.store.permit(permit.permit_id):
            self.store.insert_permit(permit)
        self.store.save_workflow_state(
            WorkflowStateRecord(
                **{
                    **state.__dict__,
                    "phase": WorkflowPhase.EXECUTION_READY,
                    "updated_at": self.clock(),
                }
            )
        )
        return self.store.permit(permit.permit_id)  # type: ignore[return-value]

    def execute_authorized(
        self,
        *,
        permit_id: str,
        live_input_provider: Callable[[], list[tuple[str, str]]] | None = None,
        **execute_kwargs,
    ) -> ExecutionResult:
        permit = self.validate_permit(permit_id)
        state = self.store.workflow_state(permit.thread_id)
        assert state is not None
        self.store.consume_permit(permit_id, consumed_at=self.clock())
        self.store.save_workflow_state(
            WorkflowStateRecord(
                **{
                    **state.__dict__,
                    "phase": WorkflowPhase.EXECUTING,
                    "updated_at": self.clock(),
                }
            )
        )
        if live_input_provider is None:

            def live_input_provider() -> list[tuple[str, str]]:
                return self.pending_live_inputs(permit.thread_id)

        result = execute_one(
            store=self.store,
            live_input_provider=live_input_provider,
            **execute_kwargs,
        )
        if result.status == "SUCCEEDED":
            current = self.store.workflow_state(permit.thread_id)
            if current:
                self.store.save_workflow_state(
                    WorkflowStateRecord(
                        **{
                            **current.__dict__,
                            "phase": WorkflowPhase.AWAITING_PUBLICATION,
                            "updated_at": self.clock(),
                        }
                    )
                )
        return result

    def pending_live_inputs(self, thread_id: str) -> list[tuple[str, str]]:
        state = self.store.workflow_state(thread_id)
        if state is None or state.phase not in (
            WorkflowPhase.PLANNING,
            WorkflowPhase.EXECUTING,
            WorkflowPhase.AWAITING_PUBLICATION,
        ):
            return []
        return [
            (
                row["event_key"],
                format_source_context(row, normalize_task(row["body"])),
            )
            for row in self.store.unconsumed_inputs(
                thread_id, after_event_key=state.root_event_key
            )
            if row["origin_surface"] == state.response_surface
            if starts_with_agent_invocation(row["body"])
            and not is_exact_approval(row["body"])
        ]

    def acknowledge_live_inputs(self, thread_id: str, *, purpose: InputPurpose) -> None:
        state = self.store.workflow_state(thread_id)
        if state is None:
            return
        for event_key, _body in self.pending_live_inputs(thread_id):
            self.store.consume_input(
                event_key,
                thread_id=thread_id,
                cycle_id=state.cycle_id,
                purpose=purpose,
                claimed_at=self.clock(),
            )

    def complete_publication(
        self,
        *,
        thread_id: str,
        publication_status: str,
        response_text: str = "",
        changed_files: tuple[str, ...] = (),
        pr_url: str | None = None,
    ) -> str | None:
        if publication_status not in {"COMPLETED", "NO_CHANGES"}:
            return None
        state = self.store.workflow_state(thread_id)
        if state is None or self.client is None:
            raise ValueError("workflow and GitHub client are required")
        marker = f"<!-- sweforge:execution-summary:{state.root_event_key} -->"
        repo = self.client.repository(state.repo_full_name)
        inline = state.response_surface == "PR_INLINE_REVIEW"
        comments = (
            self.client.review_comments_for_pull_request(repo, state.issue_number)
            if inline
            else self.client.comments(
                repo, state.response_subject_number or state.issue_number
            )
        )
        matching = [item for item in comments if marker in (item.get("body") or "")]
        if len(matching) > 1:
            raise WorkspaceError("multiple execution summary comments are ambiguous")
        if matching:
            comment_id = str(matching[0]["id"])
        else:
            body = [marker, "### SWEForge Execution", ""]
            body.append(
                "No repository changes were required."
                if publication_status == "NO_CHANGES"
                else response_text[:6_000] or "Execution completed."
            )
            if changed_files:
                body.extend(["", "Changed files:"])
                body.extend(f"- {name}" for name in changed_files[:200])
            if pr_url:
                body.extend(["", f"PR: {pr_url}"])
            rendered = "\n".join(body)[:MAX_COMMENT_CHARS]
            if inline:
                reply_to = state.review_thread_root_id or state.response_comment_id
                if not reply_to:
                    raise WorkspaceError("inline review response target is missing")
                comment_id = str(
                    self.client.create_review_comment_reply(
                        repo, int(reply_to), rendered
                    )["id"]
                )
            else:
                comment_id = str(
                    self.client.create_comment(
                        repo,
                        state.response_subject_number or state.issue_number,
                        rendered,
                    )["id"]
                )
        plan = self.store.current_plan(thread_id)
        if plan:
            self.store.update_plan(plan.plan_id, status=PlanStatus.EXECUTED)
        self.acknowledge_live_inputs(
            thread_id, purpose=InputPurpose.LIVE_EXECUTION_INPUT
        )
        self.store.save_workflow_state(
            WorkflowStateRecord(
                **{
                    **state.__dict__,
                    "phase": WorkflowPhase.IDLE,
                    "updated_at": self.clock(),
                }
            )
        )
        return comment_id

    def next_root(self, thread_id: str) -> str | None:
        state = self.store.workflow_state(thread_id)
        after = state.root_event_key if state else None
        candidates = self.store.unconsumed_inputs(thread_id, after_event_key=after)
        for row in candidates:
            if self.store.execution_for_event(row["event_key"]) is None:
                return row["event_key"]
        return None

    def begin_next_cycle(self, *, thread_id: str, **plan_kwargs) -> PlanRecord | None:
        state = self.store.workflow_state(thread_id)
        if state and state.phase != WorkflowPhase.IDLE:
            return None
        candidates = self.store.unconsumed_inputs(
            thread_id, after_event_key=state.root_event_key if state else None
        )
        for row in candidates:
            if self.store.execution_for_event(row["event_key"]) is None:
                return self.plan_event(event_key=row["event_key"], **plan_kwargs)
        return None

    def advance(
        self,
        *,
        thread_id: str,
        model: str,
        repo_paths: dict[str, str | Path],
        workspace_root: str | Path,
        memory_store: BaseStore | None = None,
        execute_kwargs: dict | None = None,
    ) -> WorkflowAdvanceResult:
        """Advance one durable worker tick for a single IssueThread.

        The per-thread caller supplies the existing execution arguments. Every
        transition is persisted before the method returns, so a later tick can
        safely recover after a process crash.
        """
        state = self.store.workflow_state(thread_id)
        if state is None or state.phase == WorkflowPhase.IDLE:
            plan = self.begin_next_cycle(
                thread_id=thread_id,
                model=model,
                repo_paths=repo_paths,
                workspace_root=workspace_root,
                memory_store=memory_store,
            )
            if plan is None:
                return WorkflowAdvanceResult(WorkflowPhase.IDLE, thread_id)
            if self.client is not None:
                plan = self.publish_plan(plan.plan_id)
            return WorkflowAdvanceResult(
                self.store.workflow_state(thread_id).phase,
                thread_id,
                plan_id=plan.plan_id,
                message="plan created",
            )

        pending = self.store.unconsumed_inputs(
            thread_id, after_event_key=state.root_event_key
        )
        pending = [
            row for row in pending if row["origin_surface"] == state.response_surface
        ]
        if state.phase == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL and pending:
            row = pending[0]
            if is_exact_approval(row["body"]):
                permit = self.approve(event_key=row["event_key"])
                return WorkflowAdvanceResult(
                    WorkflowPhase.EXECUTION_READY,
                    thread_id,
                    plan_id=permit.plan_id,
                    permit_id=permit.permit_id,
                    message="plan approved",
                )
            feedback = invocation_text(row["body"]) or row["body"]
            plan = self._replan(
                state=state,
                feedback=feedback,
                model=model,
                repo_paths=repo_paths,
                workspace_root=workspace_root,
                memory_store=memory_store,
                event_key=row["event_key"],
            )
            if self.client is not None:
                plan = self.publish_plan(plan.plan_id)
            return WorkflowAdvanceResult(
                self.store.workflow_state(thread_id).phase,
                thread_id,
                plan_id=plan.plan_id,
                message="plan revised",
            )

        if state.phase == WorkflowPhase.EXECUTION_READY:
            permit = self.store.permit_for_plan(state.current_plan_id or "")
            if permit is None:
                raise ValueError("execution-ready workflow has no permit")
            result = self.execute_authorized(
                permit_id=permit.permit_id,
                **(execute_kwargs or {}),
            )
            return WorkflowAdvanceResult(
                self.store.workflow_state(thread_id).phase,
                thread_id,
                plan_id=permit.plan_id,
                permit_id=permit.permit_id,
                execution=result,
            )
        return WorkflowAdvanceResult(state.phase, thread_id, message="waiting")

    def _replan(
        self,
        *,
        state,
        feedback: str,
        model: str,
        repo_paths: dict[str, str | Path],
        workspace_root: str | Path,
        memory_store: BaseStore | None,
        event_key: str,
    ) -> PlanRecord:
        workspace = self.store.thread_workspace(state.thread_id)
        if workspace is None:
            raise WorkspaceError("planning workspace is missing")
        root = self.store.source_event(state.root_event_key)
        if root is None:
            raise ValueError("planning root event is missing")
        context = PlannerContext(
            worktree=workspace.workspace_path,
            memory_store=memory_store,
            memory_namespace=repo_memory_namespace(state.repo_id)
            if memory_store is not None
            else None,
        )
        plan_text = self.planner(
            context=context,
            model=model,
            task=format_source_context(root, normalize_task(root["body"])),
            feedback=format_source_context(
                self.store.source_event(event_key) or {}, feedback
            ),
        )
        return self.revise(event_key=event_key, plan_text=plan_text)
