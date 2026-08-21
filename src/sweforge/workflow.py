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
            task=normalize_task(event["body"]),
        )
        if workspace.head_sha() != before_head or not workspace.is_clean():
            raise WorkspaceError("planner changed the workspace")
        return self.start_cycle(event_key=event_key, plan_text=plan_text)

    def publish_plan(self, plan_id: str) -> PlanRecord:
        plan = self.store.plan(plan_id)
        if plan is None:
            raise ValueError("unknown plan")
        if self.client is None:
            raise ValueError("GitHub client is required to publish a plan")
        repo = self.client.repository(plan.repo_full_name)
        marker = f"<!-- sweforge:plan:{plan.plan_id} -->"
        comments = self.client.comments(repo, plan.issue_number)
        matching = [item for item in comments if marker in (item.get("body") or "")]
        if len(matching) > 1:
            raise WorkspaceError("multiple plan comments are ambiguous")
        comment_id = int(matching[0]["id"]) if matching else None
        if comment_id is None:
            mode = self.store.workflow_state(plan.thread_id).mode
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
            comment_id = int(
                self.client.create_comment(repo, plan.issue_number, body)["id"]
            )
        updated = self.store.update_plan(
            plan.plan_id,
            status=PlanStatus.POSTED,
            posted_at=self.clock(),
            posted_comment_id=comment_id,
        )
        state = self.store.workflow_state(plan.thread_id)
        if state is None:
            raise ValueError("workflow state disappeared")
        self.store.save_workflow_state(
            WorkflowStateRecord(
                **{
                    **state.__dict__,
                    "phase": WorkflowPhase.WAITING_FOR_PLAN_APPROVAL,
                    "updated_at": self.clock(),
                }
            )
        )
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
        if state is None or state.phase != WorkflowPhase.WAITING_FOR_PLAN_APPROVAL:
            raise ValueError("feedback is not currently accepted for planning")
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
        self, *, permit_id: str, **execute_kwargs
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
        result = execute_one(store=self.store, **execute_kwargs)
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
