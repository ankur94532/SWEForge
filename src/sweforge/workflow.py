"""Durable planning, approval, and execution gates for IssueThreads."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from langgraph.store.base import BaseStore

from .capabilities import RepoCapabilityRegistry
from .context import RepoAgentContext
from .execution import (
    ClarificationRequestProposal,
    ExecutionResult,
    ThreadLockUnavailable,
    _execute_claim,
    normalize_task,
    run_task,
    thread_lock,
    utc_timestamp,
)
from .execution_security import SandboxBackendProvider
from .github_client import GitHubClient
from .github_models import (
    format_source_context,
    is_exact_agent_approval,
    parse_timestamp,
    starts_with_agent_invocation,
)
from .github_store import (
    AttemptKind,
    AttemptStatus,
    ClaimedEvent,
    ClarificationRequestRecord,
    ClarificationStatus,
    ExecutionPermit,
    ExecutionReviewRecord,
    InputPurpose,
    PendingWorkflowInputError,
    PermitSource,
    PlanRecord,
    PlanStatus,
    RepoMemoryLearningRecord,
    SQLiteGitHubStore,
    ThreadWorkspaceRecord,
    WorkflowMode,
    WorkflowPhase,
    WorkflowStateRecord,
)
from .memory_learning import (
    MemoryLearningResult,
    MemoryLearningStatus,
    RepoMemoryCandidate,
    apply_memory_candidates,
    curate_repository_memory,
)
from .planner import PlannerContext, generate_plan
from .reviewer import ExecutionReviewResult, ReviewerContext, review_execution
from .workspace import ThreadWorkspace, Workspace, WorkspaceError

_PREFIX_RE = re.compile(r"^\s*@agent\b", re.IGNORECASE)
MAX_COMMENT_CHARS = 12_000
MAX_REPAIR_PLAN_CHARS = 12_000
MAX_REPAIR_SUMMARY_CHARS = 2_000
MAX_REPAIR_FINDINGS_CHARS = 14_000
MAX_REPAIR_INSTRUCTIONS_CHARS = 12_000
MAX_REPAIR_REVIEW_CHARS = 30_000
MAX_REPAIR_TASK_CHARS = 50_000


@dataclass(frozen=True)
class WorkflowInputRef:
    input_id: str
    source_event_key: str
    body: str


def is_exact_approval(body: str) -> bool:
    """Only the complete ``@agent approve`` command authorizes a plan."""
    return is_exact_agent_approval(body)


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


def matches_current_conversation_target(event: dict, state) -> bool:
    if event["origin_surface"] != state.response_surface:
        return False
    if event["subject_number"] != state.response_subject_number:
        return False
    if state.response_surface == "PR_INLINE_REVIEW":
        return event["review_thread_root_id"] == state.review_thread_root_id
    return True


def _is_approval_eligible(event: dict, plan: PlanRecord) -> bool:
    if not plan.posted_at:
        return False
    created = event["source_created_at"]
    if not created:
        return False
    try:
        return parse_timestamp(created) > parse_timestamp(plan.posted_at)
    except (TypeError, ValueError):
        return False


def _is_actionable_feedback(event: dict) -> bool:
    return starts_with_agent_invocation(event["body"]) and not is_exact_approval(
        event["body"]
    )


def _stable_id(*parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return hashlib.sha256(material.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _bounded_repair_section(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n[{label} truncated at {limit} characters]"
    return text[: max(0, limit - len(marker))].rstrip() + marker


def _build_repair_task(plan: PlanRecord, parent: ExecutionReviewRecord) -> str:
    """Build a bounded application-owned task without external-input truncation."""
    summary = _bounded_repair_section(
        parent.summary, MAX_REPAIR_SUMMARY_CHARS, "review summary"
    )
    findings = _bounded_repair_section(
        parent.findings_json, MAX_REPAIR_FINDINGS_CHARS, "review findings"
    )
    instructions = _bounded_repair_section(
        parent.repair_instructions_json,
        MAX_REPAIR_INSTRUCTIONS_CHARS,
        "repair instructions",
    )
    review = (
        f"Review {parent.review_id}\n"
        f"Summary\n{summary}\n"
        f"Findings\n{findings}\n"
        f"Repair instructions\n{instructions}"
    )
    review = _bounded_repair_section(review, MAX_REPAIR_REVIEW_CHARS, "review")
    authority = (
        "Fix only the listed deficiencies within the approved plan. Inspect the "
        "current cumulative workspace, make the smallest corrections, and validate "
        "them before finishing. Do not expand scope."
    )
    suffix = f"\n\n[Execution Review NEEDS_FIXES]\n{review}\n\n[Authority]\n{authority}"
    plan_limit = min(
        MAX_REPAIR_PLAN_CHARS,
        max(0, MAX_REPAIR_TASK_CHARS - len(suffix) - 40),
    )
    plan_text = _bounded_repair_section(plan.plan_text, plan_limit, "approved plan")
    task = f"[Approved SWEForge Plan v{plan.version}]\n{plan_text}{suffix}"
    if len(task) > MAX_REPAIR_TASK_CHARS:
        raise ValueError("bounded repair task exceeded its hard maximum")
    return task


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
        reviewer: Callable[..., ExecutionReviewResult] | None = None,
        memory_learner: Callable[..., list[RepoMemoryCandidate]] | None = None,
        clarification_classifier: Callable[..., dict] | None = None,
        clock: Callable[[], str] = _now,
    ) -> None:
        self.store = store
        self.client = client
        self.planner = planner or generate_plan
        self.reviewer = reviewer or review_execution
        self.memory_learner = memory_learner
        self.clarification_classifier = clarification_classifier
        self.clock = clock

    def start_cycle(
        self,
        *,
        event_key: str,
        plan_text: str,
        root_input_id: str | None = None,
        mode: WorkflowMode = WorkflowMode.INTERACTIVE,
        posted_comment_id: int | None = None,
    ) -> PlanRecord:
        event = self.store.source_event(event_key)
        if event is None or not event["thread_id"]:
            raise ValueError("workflow root event is missing or unrouted")
        thread_id = event["thread_id"]
        thread = self.store.issue_thread(thread_id)
        if thread is None:
            raise ValueError("workflow IssueThread metadata is missing")
        state = self.store.workflow_state(thread_id)
        if state and state.phase != WorkflowPhase.IDLE:
            raise ValueError("IssueThread already has an active workflow")
        cycle_id = state.cycle_id + 1 if state else 1
        version = 1
        plan_id = "plan-" + _stable_id(thread_id, cycle_id, version, plan_text)
        timestamp = self.clock()
        record = PlanRecord(
            plan_id=plan_id,
            thread_id=thread_id,
            repo_id=thread["repo_id"],
            repo_full_name=thread["repo_full_name"],
            issue_number=thread["issue_number"],
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
            root_input_id=root_input_id,
        )
        workflow_state = WorkflowStateRecord(
            thread_id=thread_id,
            repo_id=thread["repo_id"],
            repo_full_name=thread["repo_full_name"],
            issue_number=thread["issue_number"],
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
            root_input_id=root_input_id,
        )
        self.store.begin_workflow_cycle(record, workflow_state, claimed_at=timestamp)
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
        root_input_id: str | None = None,
    ) -> PlanRecord:
        """Create a plan after proving the planner workspace stayed untouched."""
        event = self.store.source_event(event_key)
        if event is None or not event["thread_id"]:
            raise ValueError("unknown or unrouted event")
        thread = self.store.issue_thread(event["thread_id"])
        if thread is None:
            raise ValueError("workflow IssueThread metadata is missing")
        repository = repo_paths.get(event["repo_full_name"])
        if repository is None:
            raise WorkspaceError("no trusted local checkout configured")
        existing = self.store.thread_workspace(event["thread_id"])
        workspace = ThreadWorkspace.create(
            repository=Path(repository).expanduser().resolve(),
            workspace_root=workspace_root,
            repo_id=event["repo_id"],
            issue_number=thread["issue_number"],
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
                    issue_number=thread["issue_number"],
                    source_repository_path=str(Path(repository).expanduser().resolve()),
                    workspace_path=str(workspace.path),
                    branch_name=workspace.branch_name,
                    base_commit=workspace.base_commit,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        before_head = workspace.head_sha()
        selected_text = (
            self.store.deferred_text_for_event(event_key, deferred_id=root_input_id)
            if root_input_id and root_input_id.startswith("deferred-")
            else None
        )
        mode = WorkflowMode.INTERACTIVE
        if self.client is not None:
            repo = self.client.repository(thread["repo_full_name"])
            if issue_has_auto_label(self.client.issue(repo, thread["issue_number"])):
                mode = WorkflowMode.AUTO
        existing_state = self.store.workflow_state(event["thread_id"])
        existing_plan = self.store.current_plan(event["thread_id"])
        if (
            existing_state is not None
            and existing_state.phase == WorkflowPhase.PLANNING
            and existing_plan is not None
            and existing_plan.root_event_key == event_key
            and existing_plan.root_input_id == root_input_id
        ):
            draft = existing_plan
        else:
            draft = self.start_cycle(
                event_key=event_key,
                plan_text="Planning in progress",
                mode=mode,
                root_input_id=root_input_id,
            )
        delivered: set[str] = set()
        context = PlannerContext(
            worktree=str(workspace.path),
            repo_context=RepoAgentContext(
                repo_id=event["repo_id"],
                repo_full_name=event["repo_full_name"],
                thread_id=event["thread_id"],
            ),
            memory_store=memory_store,
            memory_namespace=None,
            live_input_provider=lambda: self.pending_planning_inputs(
                event["thread_id"]
            ),
            live_delivered_event_keys=delivered,
        )
        plan_text = (planner or self.planner)(
            context=context,
            model=model,
            task=format_source_context(
                event, normalize_task(selected_text or event["body"])
            ),
        )
        if workspace.head_sha() != before_head or not workspace.is_clean():
            raise WorkspaceError("planner changed the workspace")
        self.store.update_plan(draft.plan_id, plan_text=plan_text)
        for event_key in delivered:
            self._acknowledge_delivered(
                event_key,
                thread_id=event["thread_id"],
                cycle_id=draft.cycle_id,
                purpose=InputPurpose.PLANNING_INPUT,
            )
        return self.store.plan(draft.plan_id)  # type: ignore[return-value]

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
            self.client.review_comments_for_pull_request(
                repo, state.response_subject_number or plan.issue_number
            )
            if inline
            else self.client.comments(
                repo, state.response_subject_number or plan.issue_number
            )
        )
        matching = [item for item in comments if marker in (item.get("body") or "")]
        if len(matching) > 1:
            raise WorkspaceError("multiple plan comments are ambiguous")
        comment = matching[0] if matching else None
        comment_id = int(comment["id"]) if comment else None
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
                comment = self.client.create_review_comment_reply(
                    repo,
                    state.response_subject_number or plan.issue_number,
                    int(reply_to),
                    body,
                )
                comment_id = int(comment["id"])
            else:
                comment = self.client.create_comment(
                    repo, state.response_subject_number or plan.issue_number, body
                )
                comment_id = int(comment["id"])
        posted_at = (comment or {}).get("created_at") or self.clock()
        updated = self.store.mark_current_plan_posted(
            plan.plan_id,
            comment_id=comment_id,
            posted_at=posted_at,
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
        if state is None:
            self._acknowledge_delivered(
                event_key,
                thread_id=event["thread_id"],
                cycle_id=0,
                purpose=InputPurpose.STALE_PLAN_APPROVAL,
            )
            raise ValueError("approval is not currently actionable")
        if state.phase != WorkflowPhase.WAITING_FOR_PLAN_APPROVAL:
            self._acknowledge_delivered(
                event_key,
                thread_id=state.thread_id,
                cycle_id=state.cycle_id,
                purpose=InputPurpose.STALE_PLAN_APPROVAL,
            )
            raise ValueError("approval is only valid while waiting for a posted plan")
        if not matches_current_conversation_target(event, state):
            self._acknowledge_delivered(
                event_key,
                thread_id=state.thread_id,
                cycle_id=state.cycle_id,
                purpose=InputPurpose.STALE_PLAN_APPROVAL,
            )
            raise ValueError("approval came from a different conversation target")
        plan = self.store.current_plan(state.thread_id)
        if plan is None or plan.status != PlanStatus.POSTED:
            raise ValueError("no current posted plan can be approved")
        if not _is_approval_eligible(event, plan):
            self._acknowledge_delivered(
                event_key,
                thread_id=state.thread_id,
                cycle_id=state.cycle_id,
                purpose=InputPurpose.EARLY_PLAN_APPROVAL,
            )
            raise ValueError("approval predates the visible plan")
        timestamp = self.clock()
        permit = ExecutionPermit(
            permit_id="permit-"
            + _stable_id(
                state.thread_id, state.cycle_id, plan.plan_id, "USER", event_key
            ),
            thread_id=state.thread_id,
            cycle_id=state.cycle_id,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            root_event_key=plan.root_event_key,
            source=PermitSource.USER,
            source_event_key=event_key,
            created_at=timestamp,
            consumed_at=None,
            invalidated_at=None,
        )
        return self.store.approve_current_plan(
            event_key=event_key,
            author_login=author_login or event["author_login"],
            permit=permit,
            approved_at=timestamp,
        )

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
        if not matches_current_conversation_target(event, state):
            raise ValueError("feedback came from a different conversation target")
        current = self.store.current_plan(state.thread_id)
        if current is None:
            raise ValueError("current plan is missing")
        timestamp = self.clock()
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
            root_input_id=current.root_input_id,
        )
        self.store.begin_plan_revision(event_key, now=timestamp)
        return self.store.finish_plan_revision(
            plan=plan,
            feedback_event_key=event_key,
            finished_at=timestamp,
        )

    def validate_permit(self, permit_id: str) -> ExecutionPermit:
        permit = self.store.permit(permit_id)
        if permit is None or permit.invalidated_at:
            raise ValueError("execution permit is unavailable")
        execution = self.store.execution_for_cycle(
            thread_id=permit.thread_id,
            cycle_id=permit.cycle_id,
            root_event_key=permit.root_event_key,
            root_input_id=(
                self.store.plan(permit.plan_id).root_input_id
                if self.store.plan(permit.plan_id)
                else None
            ),
        )
        reusable_retry = bool(
            permit.consumed_at and execution and execution["status"] == "RETRY_PENDING"
        )
        if permit.consumed_at and not reusable_retry:
            raise ValueError("execution permit is unavailable")
        state = self.store.workflow_state(permit.thread_id)
        plan = self.store.plan(permit.plan_id)
        if state is None or plan is None:
            raise ValueError("execution permit references missing workflow data")
        if state.phase != WorkflowPhase.EXECUTION_READY:
            raise ValueError("workflow is not ready for execution")
        if state.cycle_id != permit.cycle_id or state.current_plan_id != permit.plan_id:
            raise ValueError("execution permit is stale")
        if permit.root_event_key != plan.root_event_key:
            raise ValueError("execution permit root event is stale")
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
        permit = ExecutionPermit(
            permit_id="permit-"
            + _stable_id(thread_id, state.cycle_id, plan.plan_id, "AUTO"),
            thread_id=thread_id,
            cycle_id=state.cycle_id,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            root_event_key=plan.root_event_key,
            source=PermitSource.AUTO,
            source_event_key=None,
            created_at=timestamp,
            consumed_at=None,
            invalidated_at=None,
        )
        existing = self.store.permit(permit.permit_id)
        if existing:
            return existing
        return self.store.authorize_current_plan(
            permit=permit,
            authorized_at=timestamp,
        )

    def execute_authorized(
        self,
        *,
        permit_id: str,
        model: str,
        repo_paths: dict[str, str | Path],
        workspace_root: str | Path,
        lock_root: str | Path,
        checkpointer: object,
        runner: Callable[..., str] | None = None,
        memory_store: BaseStore | None = None,
        live_input_provider: Callable[[], list[tuple[str, str]]] | None = None,
        now: Callable[[], datetime] | None = None,
        capability_registry: RepoCapabilityRegistry | None = None,
        sandbox_backend_provider: SandboxBackendProvider | None = None,
        secure_execution: bool = True,
        unsafe_local_shell: bool = False,
    ) -> ExecutionResult:
        permit = self.validate_permit(permit_id)
        delivered: set[str] = set()
        clock = now or (lambda: datetime.now(UTC))
        try:
            with thread_lock(lock_root, permit.thread_id):
                event = self.store.bind_authorized_execution(
                    permit_id,
                    expected_thread_id=permit.thread_id,
                    now=utc_timestamp(clock()),
                )
                plan = self.store.plan(permit.plan_id)
                if plan is None:
                    raise ValueError("approved plan disappeared")
                attempt_id = f"attempt-{permit.permit_id}"
                attempt = self.store.ensure_execution_attempt(
                    attempt_id=attempt_id,
                    thread_id=permit.thread_id,
                    cycle_id=permit.cycle_id,
                    plan_id=plan.plan_id,
                    plan_version=plan.version,
                    root_event_key=plan.root_event_key,
                    authorization_id=permit.permit_id,
                    created_at=utc_timestamp(clock()),
                )
                answered = self.store.clarification_for_thread(
                    permit.thread_id, status=ClarificationStatus.ANSWERED.value
                )
                resume_value = None
                if answered and answered.cycle_id == permit.cycle_id:
                    answer_payload = json.loads(answered.answer_json or "{}")
                    resume_value = answer_payload.get("answer")

                def persist_request(payload: dict) -> None:
                    current_state = self.store.workflow_state(permit.thread_id)
                    if current_state is None:
                        raise ValueError(
                            "workflow state disappeared during clarification"
                        )
                    self._persist_clarification(
                        state=current_state,
                        proposal=ClarificationRequestProposal(
                            question=payload["question"],
                            reason=payload["reason"],
                            answer_type=payload["answer_type"],
                            choices=tuple(payload["choices"]),
                            occurrence_key=payload.get("occurrence_key", ""),
                        ),
                    )

                result = _execute_claim(
                    store=self.store,
                    event=event,
                    live_input_provider=None,
                    live_delivered_event_keys=delivered,
                    clarification_request_sink=persist_request,
                    resume_value=resume_value,
                    approved_plan_text=plan.plan_text,
                    approved_plan_id=plan.plan_id,
                    approved_plan_version=plan.version,
                    deferred_id=plan.root_input_id
                    if plan.root_input_id and plan.root_input_id.startswith("deferred-")
                    else None,
                    now=clock,
                    model=model,
                    repo_paths=repo_paths,
                    workspace_root=workspace_root,
                    checkpointer=checkpointer,
                    runner=runner or run_task,
                    memory_store=memory_store,
                    capability_registry=capability_registry,
                    sandbox_backend_provider=sandbox_backend_provider,
                    secure_execution=secure_execution,
                    unsafe_local_shell=unsafe_local_shell,
                )
                for event_key in delivered:
                    self._acknowledge_delivered(
                        event_key, thread_id=permit.thread_id, cycle_id=permit.cycle_id
                    )
                current = self.store.workflow_state(permit.thread_id)
                if result.status == "SUCCEEDED":
                    execution = self.store.execution_for_cycle(
                        thread_id=permit.thread_id,
                        cycle_id=permit.cycle_id,
                        root_event_key=permit.root_event_key,
                        root_input_id=plan.root_input_id,
                    )
                    self.store.finish_execution_attempt(
                        attempt.attempt_id,
                        status=AttemptStatus.SUCCEEDED,
                        completed_at=utc_timestamp(clock()),
                        response_text=execution["response_text"] if execution else None,
                        start_head_sha=execution["start_head_sha"]
                        if execution
                        else None,
                        end_head_sha=execution["end_head_sha"] if execution else None,
                        end_dirty=bool(execution["end_dirty"]) if execution else False,
                    )
                elif result.status == "CLARIFICATION":
                    self.store.finish_execution_attempt(
                        attempt.attempt_id,
                        status=AttemptStatus.INTERRUPTED,
                        completed_at=utc_timestamp(clock()),
                        response_text=result.response,
                    )
                    self.store.interrupt_execution_for_clarification(
                        permit_id=permit.permit_id,
                        event_key=permit.root_event_key,
                        now=utc_timestamp(clock()),
                    )
                    current = self.store.workflow_state(permit.thread_id)
                    if current and result.clarification:
                        self._persist_clarification(
                            state=current, proposal=result.clarification
                        )
                else:
                    self.store.finish_execution_attempt(
                        attempt.attempt_id,
                        status=AttemptStatus.FAILED,
                        completed_at=utc_timestamp(clock()),
                        response_text=result.error,
                    )
                if current:
                    phase = (
                        WorkflowPhase.REVIEW_EXECUTION
                        if result.status == "SUCCEEDED"
                        else WorkflowPhase.WAITING_FOR_INPUT
                        if result.status == "CLARIFICATION"
                        else WorkflowPhase.EXECUTION_READY
                    )
                    self.store.save_workflow_state(
                        WorkflowStateRecord(
                            **{
                                **current.__dict__,
                                "phase": phase,
                                "updated_at": self.clock(),
                            }
                        )
                    )
                return result
        except ThreadLockUnavailable:
            return ExecutionResult(status="BUSY")

    def _persist_clarification(
        self, *, state: WorkflowStateRecord, proposal: ClarificationRequestProposal
    ) -> ClarificationRequestRecord:
        clarification_id = "clarification-" + _stable_id(
            state.thread_id,
            state.cycle_id,
            proposal.occurrence_key or proposal.question,
        )
        record = ClarificationRequestRecord(
            clarification_id=clarification_id,
            thread_id=state.thread_id,
            cycle_id=state.cycle_id,
            root_event_key=state.root_event_key,
            occurrence_key=proposal.occurrence_key,
            requested_from_phase=WorkflowPhase.EXECUTING.value,
            question=proposal.question,
            reason=proposal.reason,
            answer_type=proposal.answer_type,
            choices_json=json.dumps(list(proposal.choices)),
            origin_surface=state.response_surface,
            response_subject_number=state.response_subject_number or state.issue_number,
            response_comment_id=None,
            response_url=None,
            review_thread_root_id=state.review_thread_root_id,
            status=ClarificationStatus.OPEN.value,
            created_at=self.clock(),
            answered_at=None,
            answer_event_key=None,
            answer_json=None,
        )
        existing = self.store.clarification(clarification_id)
        if existing and existing.response_comment_id:
            return existing
        saved = self.store.save_clarification(record)
        if self.client is None:
            return saved
        repo = self.client.repository(state.repo_full_name)
        marker = f"<!-- sweforge:clarification:{clarification_id} -->"
        existing_comments = (
            self.client.review_comments_for_pull_request(
                repo, saved.response_subject_number
            )
            if state.response_surface == "PR_INLINE_REVIEW"
            else self.client.comments(repo, saved.response_subject_number)
        )
        matching = [
            item for item in existing_comments if marker in (item.get("body") or "")
        ]
        if matching:
            return self.store.save_clarification(
                replace(
                    saved,
                    response_comment_id=str(matching[0]["id"]),
                    response_url=matching[0].get("html_url"),
                )
            )
        choices = json.loads(saved.choices_json)
        body = [
            marker,
            "### SWEForge needs one detail before continuing",
            "",
            saved.question,
        ]
        if choices:
            body.extend(["", *[f"- {choice}" for choice in choices]])
        body.extend(["", "Reply with:", "@agent <your answer>"])
        rendered = "\n".join(body)
        if state.response_surface == "PR_INLINE_REVIEW":
            root = state.review_thread_root_id or state.response_comment_id
            if not root:
                raise WorkspaceError("inline clarification response target is missing")
            comment = self.client.create_review_comment_reply(
                repo, saved.response_subject_number, int(root), rendered
            )
        else:
            comment = self.client.create_comment(
                repo, saved.response_subject_number, rendered
            )
        updated = replace(
            saved,
            response_comment_id=str(comment["id"]),
            response_url=comment.get("html_url"),
        )
        return self.store.save_clarification(updated)

    def execute_repair_authorized(
        self,
        *,
        permit_id: str,
        model: str,
        repo_paths: dict[str, str | Path],
        workspace_root: str | Path,
        lock_root: str | Path,
        checkpointer: object,
        runner: Callable[..., str] | None = None,
        memory_store: BaseStore | None = None,
        live_input_provider: Callable[[], list[tuple[str, str]]] | None = None,
        now: Callable[[], datetime] | None = None,
        capability_registry: RepoCapabilityRegistry | None = None,
        sandbox_backend_provider: SandboxBackendProvider | None = None,
        secure_execution: bool = True,
        unsafe_local_shell: bool = False,
    ) -> ExecutionResult:
        """Run a review-authorized repair in the existing cumulative workspace."""
        permit = self.store.repair_permit(permit_id)
        if permit is None:
            raise ValueError("repair permit is unavailable")
        source = self.store.source_event(permit.root_event_key)
        workspace = self.store.thread_workspace(permit.thread_id)
        thread = self.store.issue_thread(permit.thread_id)
        plan = self.store.plan(permit.plan_id)
        if source is None or workspace is None or thread is None or plan is None:
            raise ValueError("repair workspace or root event is unavailable")
        event = ClaimedEvent(
            event_key=source["event_key"],
            thread_id=source["thread_id"],
            repo_id=source["repo_id"],
            repo_full_name=source["repo_full_name"],
            issue_number=thread["issue_number"],
            body=source["body"],
            workspace_path=workspace.workspace_path,
            retrying=True,
            origin_surface=source["origin_surface"],
            path=source["path"],
            line=source["line"],
            start_line=source["start_line"],
            side=source["side"],
            start_side=source["start_side"],
            diff_hunk=source["diff_hunk"],
            commit_id=source["commit_id"],
            original_commit_id=source["original_commit_id"],
            in_reply_to_id=source["in_reply_to_id"],
            pull_request_review_id=source["pull_request_review_id"],
            review_thread_root_id=source["review_thread_root_id"],
        )
        clock = now or (lambda: datetime.now(UTC))
        parent = self.store.execution_review(permit.parent_review_id)
        if parent is None:
            raise ValueError("repair permit parent review is unavailable")
        task = _build_repair_task(plan, parent)
        delivered: set[str] = set()
        try:
            with thread_lock(lock_root, permit.thread_id):
                attempt = self.store.bind_repair_execution(
                    permit_id,
                    expected_thread_id=permit.thread_id,
                    now=utc_timestamp(clock()),
                )
                result = _execute_claim(
                    store=self.store,
                    event=event,
                    model=model,
                    repo_paths=repo_paths,
                    workspace_root=workspace_root,
                    checkpointer=checkpointer,
                    runner=runner or run_task,
                    memory_store=memory_store,
                    live_input_provider=None,
                    live_delivered_event_keys=delivered,
                    clarification_enabled=False,
                    approved_plan_text=None,
                    approved_plan_id=None,
                    approved_plan_version=None,
                    now=clock,
                    persist_execution=False,
                    allow_dirty_workspace=True,
                    message_id="sweforge:review-repair:"
                    + hashlib.sha256(permit_id.encode()).hexdigest(),
                    prepared_task=task,
                    capability_registry=capability_registry,
                    sandbox_backend_provider=sandbox_backend_provider,
                    secure_execution=secure_execution,
                    unsafe_local_shell=unsafe_local_shell,
                )
                for event_key in delivered:
                    self._acknowledge_delivered(
                        event_key, thread_id=permit.thread_id, cycle_id=permit.cycle_id
                    )
                if result.status == "SUCCEEDED" and result.workspace:
                    self.store.finish_repair_attempt_success(
                        permit_id,
                        attempt_id=attempt.attempt_id,
                        now=utc_timestamp(clock()),
                        response_text=result.response,
                        end_head_sha=result.workspace.head_sha(),
                        end_dirty=not result.workspace.is_clean(),
                        workspace_path=str(result.workspace.path),
                        start_head_sha=self.store.execution_for_event(
                            permit.root_event_key
                        )["start_head_sha"],
                    )
                else:
                    self.store.mark_repair_attempt_failed(
                        attempt.attempt_id, now=utc_timestamp(clock())
                    )
                return result
        except PendingWorkflowInputError:
            return ExecutionResult(status="BLOCKED", event=event)
        except ThreadLockUnavailable:
            return ExecutionResult(status="BUSY", event=event)

    def _acknowledge_delivered(
        self,
        input_id: str,
        *,
        thread_id: str,
        cycle_id: int,
        purpose: InputPurpose = InputPurpose.DEFERRED_FOLLOWUP,
    ) -> None:
        if purpose == InputPurpose.PLANNING_INPUT and not input_id.startswith(
            "deferred-"
        ):
            legacy = self.store.deferred_followup(input_id)
            if legacy is not None:
                input_id = legacy.deferred_id
        if purpose == InputPurpose.PLANNING_INPUT and input_id.startswith("deferred-"):
            self.store.consume_deferred_followup(
                self.store.deferred_followup_by_id(input_id)["event_key"],
                deferred_id=input_id,
                cycle_id=cycle_id,
                consumed_at=self.clock(),
            )
            return
        self.store.consume_input(
            input_id,
            thread_id=thread_id,
            cycle_id=cycle_id,
            purpose=purpose,
            claimed_at=self.clock(),
        )

    def pending_live_inputs(self, thread_id: str) -> list[tuple[str, str]]:
        state = self.store.workflow_state(thread_id)
        if state is None or state.phase not in (
            WorkflowPhase.PLANNING,
            WorkflowPhase.EXECUTING,
            WorkflowPhase.REVIEW_EXECUTION,
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
            if starts_with_agent_invocation(row["body"])
            and not is_exact_approval(row["body"])
        ]

    def pending_planning_inputs(self, thread_id: str) -> list[tuple[str, str]]:
        state = self.store.workflow_state(thread_id)
        after = state.root_event_key if state else None
        rows = list(self.store.deferred_followups(thread_id))
        rows.extend(self.store.unconsumed_inputs(thread_id, after_event_key=after))
        seen: set[str] = set()
        result: list[tuple[str, str]] = []
        for row in rows:
            logical_id = (
                row["deferred_id"] if "deferred_id" in row.keys() else row["event_key"]
            )
            if (
                logical_id in seen
                or not starts_with_agent_invocation(row["body"])
                or is_exact_approval(row["body"])
            ):
                continue
            seen.add(logical_id)
            result.append(
                (
                    logical_id,
                    format_source_context(row, normalize_task(row["body"])),
                )
            )
        return result

    def _defer_active_followups(self, state: WorkflowStateRecord) -> None:
        if state.phase not in {
            WorkflowPhase.EXECUTING,
            WorkflowPhase.REVIEW_EXECUTION,
            WorkflowPhase.REPAIR_READY,
            WorkflowPhase.AWAITING_PUBLICATION,
        }:
            return
        for row in self.store.unconsumed_inputs(
            state.thread_id, after_event_key=state.root_event_key
        ):
            if _is_actionable_feedback(row):
                self.store.defer_followup(
                    source_event_key=row["event_key"],
                    thread_id=state.thread_id,
                    originating_cycle_id=state.cycle_id,
                    queued_at=self.clock(),
                )

    @staticmethod
    def _clarification_target_matches(row, clarification) -> bool:
        same_surface = row["origin_surface"] == clarification.origin_surface
        same_subject = row["subject_number"] == clarification.response_subject_number
        if clarification.origin_surface == "PR_INLINE_REVIEW":
            same_target = (
                row["review_thread_root_id"] == clarification.review_thread_root_id
            )
        else:
            same_target = same_surface and same_subject
        if not same_target:
            return False
        if clarification.clarification_id in row["body"]:
            return True
        return (
            bool(
                row["in_reply_to_id"]
                and clarification.response_comment_id
                and row["in_reply_to_id"] == clarification.response_comment_id
            )
            or same_target
        )

    def _deterministic_clarification_answer(
        self, clarification, body: str
    ) -> str | None:
        text = invocation_text(body) or body
        answer_type = clarification.answer_type.upper()
        if answer_type == "BOOLEAN":
            lowered = text.casefold().strip(" .!?\t\n")
            if lowered in {"yes", "no", "true", "false"}:
                return "true" if lowered in {"yes", "true"} else "false"
            return None
        if answer_type == "CHOICE":
            choices = json.loads(clarification.choices_json)
            matches = [
                choice for choice in choices if choice.casefold() in text.casefold()
            ]
            return matches[0] if len(matches) == 1 else None
        if answer_type in {"TEXT", "VALUE"}:
            return None
        return None

    def _resolve_open_clarification(self, state: WorkflowStateRecord) -> None:
        clarification = self.store.clarification_for_thread(state.thread_id)
        if clarification is None:
            return
        for row in self.store.unconsumed_inputs(
            state.thread_id, after_event_key=state.root_event_key
        ):
            if not starts_with_agent_invocation(row["body"]):
                continue
            if not self._clarification_target_matches(row, clarification):
                self.store.defer_followup(
                    source_event_key=row["event_key"],
                    thread_id=state.thread_id,
                    originating_cycle_id=state.cycle_id,
                    queued_at=self.clock(),
                )
                continue
            answer = self._deterministic_clarification_answer(
                clarification, row["body"]
            )
            classification = None
            if answer is None and self.clarification_classifier is not None:
                classification = self.clarification_classifier(
                    clarification=clarification, event=dict(row)
                )
                if classification.get("relationship") == "CHANGES_SCOPE":
                    self.store.cancel_clarification_for_replan(
                        clarification.clarification_id,
                        event_key=row["event_key"],
                        now=self.clock(),
                    )
                    return
                if classification.get("relationship") != "ANSWERS_CLARIFICATION":
                    if classification.get("relationship") in {
                        "UNRELATED_FOLLOWUP",
                        "CHANGES_SCOPE",
                    }:
                        self.store.defer_followup(
                            source_event_key=row["event_key"],
                            thread_id=state.thread_id,
                            originating_cycle_id=state.cycle_id,
                            queued_at=self.clock(),
                        )
                    continue
                answer = classification.get("extracted_answer")
            if not answer:
                continue
            residual = None
            text = invocation_text(row["body"]) or row["body"]
            if answer.casefold() in text.casefold():
                residual = text[text.casefold().find(answer.casefold()) + len(answer) :]
                residual = residual.lstrip(" .,:;!-\n")
                if residual.casefold().startswith(("also ", "and ", "plus ")):
                    residual = residual.split(" ", 1)[1].strip()
                else:
                    residual = None
            if residual:
                residual = residual[:MAX_COMMENT_CHARS]
            self.store.resolve_clarification_answer(
                clarification.clarification_id,
                answer_event_key=row["event_key"],
                answer_json=json.dumps({"answer": answer, "residual": residual}),
                residual_text=residual,
                now=self.clock(),
            )
            return

    def acknowledge_live_inputs(
        self,
        thread_id: str,
        *,
        purpose: InputPurpose,
        event_keys: set[str],
    ) -> None:
        state = self.store.workflow_state(thread_id)
        if state is None:
            return
        for event_key in event_keys:
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
        memory_store: BaseStore | None = None,
        memory_lock_root: str | Path = "~/.sweforge/locks",
        memory_model: str | None = None,
    ) -> str | None:
        if publication_status not in {"COMPLETED", "NO_CHANGES"}:
            return None
        state = self.store.workflow_state(thread_id)
        if state is None or self.client is None:
            raise ValueError("workflow and GitHub client are required")
        if not self.store.publication_is_eligible(state.root_event_key):
            raise ValueError("execution summary is blocked until review ACCEPT")
        marker = f"<!-- sweforge:execution-summary:{state.root_event_key} -->"
        repo = self.client.repository(state.repo_full_name)
        inline = state.response_surface == "PR_INLINE_REVIEW"
        comments = (
            self.client.review_comments_for_pull_request(
                repo, state.response_subject_number or state.issue_number
            )
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
            body = [marker, "### SWEForge Execution", "", "Execution review: accepted."]
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
                        repo,
                        state.response_subject_number or state.issue_number,
                        int(reply_to),
                        rendered,
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
        learning_now = self.clock()
        self.store.save_repo_memory_learning(
            RepoMemoryLearningRecord(
                event_key=state.root_event_key,
                thread_id=state.thread_id,
                cycle_id=state.cycle_id,
                repo_id=state.repo_id,
                status=MemoryLearningStatus.PENDING.value,
                accepted_candidates=0,
                rejected_candidates=0,
                error_message=None,
                created_at=learning_now,
                updated_at=learning_now,
                proposal_json="{}",
            )
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
        workspace = self.store.thread_workspace(thread_id)
        self._learn_repository_memory(
            state=state,
            workspace_path=workspace.workspace_path if workspace else None,
            memory_store=memory_store,
            lock_root=memory_lock_root,
            memory_model=memory_model,
        )
        return comment_id

    def _learn_repository_memory(
        self,
        *,
        state: WorkflowStateRecord,
        workspace_path: str | None,
        memory_store: BaseStore | None,
        lock_root: str | Path,
        memory_model: str | None,
    ) -> None:
        now = self.clock()
        existing = self.store.repo_memory_learning(state.root_event_key)
        proposal_json = existing.proposal_json if existing else "{}"
        if existing is not None and existing.status in {
            MemoryLearningStatus.UPDATED.value,
            MemoryLearningStatus.NO_UPDATE.value,
        }:
            return
        try:
            if proposal_json != "{}":
                candidates = [
                    RepoMemoryCandidate.model_validate(item)
                    for item in json.loads(proposal_json)
                ]
            elif self.memory_learner and workspace_path:
                candidates = self.memory_learner(
                    state=state,
                    worktree=workspace_path,
                )
                proposal_json = json.dumps(
                    [candidate.model_dump() for candidate in candidates], sort_keys=True
                )
            elif memory_model and workspace_path and memory_store is not None:
                workspace_record = self.store.thread_workspace(state.thread_id)
                plan = self.store.current_plan(state.thread_id)
                if workspace_record is None:
                    raise ValueError("repository workspace metadata is missing")
                inspection = Workspace(
                    Path(workspace_path),
                    Path(workspace_path),
                    workspace_record.base_commit,
                )
                from .repo_memory import read_repo_memory, repo_memory_namespace

                curator_output = curate_repository_memory(
                    model=memory_model,
                    repo_id=state.repo_id,
                    worktree=workspace_path,
                    changed_files=inspection.changed_files()[:100],
                    diff=inspection.diff()[:40_000],
                    existing_memory=read_repo_memory(
                        memory_store, repo_memory_namespace(state.repo_id)
                    )
                    or "",
                    plan_text=plan.plan_text if plan else "",
                )
                candidates = curator_output.candidates
                proposal_json = curator_output.proposal_json
            else:
                candidates = []
                proposal_json = "[]"
            self.store.save_repo_memory_learning(
                RepoMemoryLearningRecord(
                    event_key=state.root_event_key,
                    thread_id=state.thread_id,
                    cycle_id=state.cycle_id,
                    repo_id=state.repo_id,
                    status=MemoryLearningStatus.PENDING.value,
                    accepted_candidates=0,
                    rejected_candidates=0,
                    error_message=None,
                    created_at=existing.created_at if existing else now,
                    updated_at=now,
                    proposal_json=proposal_json,
                )
            )
            if memory_store is None or not workspace_path:
                result = MemoryLearningResult(
                    MemoryLearningStatus.NO_UPDATE,
                    rejected_candidates=len(candidates),
                )
            else:
                result = apply_memory_candidates(
                    memory_store,
                    repo_id=state.repo_id,
                    worktree=workspace_path,
                    candidates=candidates,
                    lock_root=lock_root,
                )
        except Exception as exc:
            result = MemoryLearningResult(
                MemoryLearningStatus.FAILED,
                error=str(exc)[:500],
            )
        self.store.save_repo_memory_learning(
            RepoMemoryLearningRecord(
                event_key=state.root_event_key,
                thread_id=state.thread_id,
                cycle_id=state.cycle_id,
                repo_id=state.repo_id,
                status=result.status.value,
                accepted_candidates=result.accepted_candidates,
                rejected_candidates=result.rejected_candidates,
                error_message=result.error,
                created_at=existing.created_at if existing else now,
                updated_at=now,
                proposal_json=proposal_json,
            )
        )

    def post_blocked_review_comment(
        self, thread_id: str, *, summary: str
    ) -> str | None:
        state = self.store.workflow_state(thread_id)
        if state is None or self.client is None:
            return None
        marker = (
            "<!-- sweforge:execution-review-blocked:"
            f"{state.cycle_id}:{state.current_plan_id} -->"
        )
        repo = self.client.repository(state.repo_full_name)
        number = state.response_subject_number or state.issue_number
        comments = (
            self.client.review_comments_for_pull_request(repo, number)
            if state.response_surface == "PR_INLINE_REVIEW"
            else self.client.comments(repo, number)
        )
        if any(marker in (item.get("body") or "") for item in comments):
            return None
        body = (
            f"{marker}\n### SWEForge execution review needs attention\n\n"
            "The implementation was not published because execution review could "
            "not confirm that the approved plan was satisfied.\n\n"
            f"{summary[:4_000]}"
        )
        if state.response_surface == "PR_INLINE_REVIEW":
            root = state.review_thread_root_id or state.response_comment_id
            if not root:
                raise WorkspaceError("inline review response target is missing")
            return str(
                self.client.create_review_comment_reply(repo, number, int(root), body)[
                    "id"
                ]
            )
        return str(self.client.create_comment(repo, number, body)["id"])

    def _drain_approval_controls(self, thread_id: str, state=None) -> None:
        state = state or self.store.workflow_state(thread_id)
        after = state.root_event_key if state else None
        plan = self.store.current_plan(thread_id) if state else None
        for row in self.store.unconsumed_inputs(thread_id, after_event_key=after):
            if not is_exact_agent_approval(row["body"]):
                continue
            eligible = bool(
                state
                and state.phase == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL
                and plan is not None
                and matches_current_conversation_target(row, state)
                and _is_approval_eligible(row, plan)
            )
            if eligible:
                continue
            purpose = InputPurpose.STALE_PLAN_APPROVAL
            if state and state.phase == WorkflowPhase.PLANNING:
                purpose = InputPurpose.EARLY_PLAN_APPROVAL
            elif (
                state
                and state.phase == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL
                and matches_current_conversation_target(row, state)
            ):
                purpose = InputPurpose.EARLY_PLAN_APPROVAL
            self._acknowledge_delivered(
                row["event_key"],
                thread_id=thread_id,
                cycle_id=state.cycle_id if state else 0,
                purpose=purpose,
            )

    def _recover_initial_execution(self, state: WorkflowStateRecord):
        """Recover only an INITIAL attempt from root execution evidence."""
        if hasattr(self.store, "execution_for_cycle"):
            execution = self.store.execution_for_cycle(
                thread_id=state.thread_id,
                cycle_id=state.cycle_id,
                root_event_key=state.root_event_key,
                root_input_id=state.root_input_id,
            )
        else:
            execution = self.store.execution_for_event(state.root_event_key)
        if not execution or execution["status"] != "SUCCEEDED":
            return state
        latest = self.store.latest_attempt(state.thread_id, state.cycle_id)
        if latest and latest.kind is not AttemptKind.INITIAL:
            return state
        plan = self.store.current_plan(state.thread_id)
        permit = self.store.permit_for_plan(plan.plan_id) if plan else None
        if (
            plan is None
            or permit is None
            or permit.thread_id != state.thread_id
            or permit.cycle_id != state.cycle_id
            or permit.plan_id != plan.plan_id
            or permit.plan_version != plan.version
            or permit.root_event_key != state.root_event_key
            or plan.status not in (PlanStatus.APPROVED, PlanStatus.AUTO_APPROVED)
        ):
            self.store.save_workflow_state(
                replace(
                    state,
                    phase=WorkflowPhase.REVIEW_BLOCKED,
                    updated_at=self.clock(),
                )
            )
            return self.store.workflow_state(state.thread_id)
        attempt = self.store.latest_attempt(state.thread_id, state.cycle_id)
        if attempt is None:
            attempt = self.store.ensure_execution_attempt(
                attempt_id=f"attempt-{permit.permit_id}",
                thread_id=state.thread_id,
                cycle_id=state.cycle_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                root_event_key=state.root_event_key,
                authorization_id=permit.permit_id,
                created_at=self.clock(),
                attempt_number=1,
            )
        if attempt.status != AttemptStatus.SUCCEEDED:
            self.store.finish_execution_attempt(
                attempt.attempt_id,
                status=AttemptStatus.SUCCEEDED,
                completed_at=execution["completed_at"] or self.clock(),
                response_text=execution["response_text"],
                start_head_sha=execution["start_head_sha"],
                end_head_sha=execution["end_head_sha"],
                end_dirty=bool(execution["end_dirty"]),
            )
        self.store.save_workflow_state(
            replace(
                state,
                phase=WorkflowPhase.REVIEW_EXECUTION,
                updated_at=self.clock(),
            )
        )
        return self.store.workflow_state(state.thread_id)

    def _recover_executing_state(
        self, state: WorkflowStateRecord, *, lock_root: str | Path
    ) -> tuple[WorkflowStateRecord, bool]:
        """Recover EXECUTING without treating repair work as root success."""
        if state.phase != WorkflowPhase.EXECUTING:
            return state, False
        latest = self.store.latest_attempt(state.thread_id, state.cycle_id)
        if latest and latest.kind is AttemptKind.REVIEW_REPAIR:
            try:
                with thread_lock(lock_root, state.thread_id):
                    fresh = self.store.workflow_state(state.thread_id)
                    current = (
                        self.store.latest_attempt(state.thread_id, state.cycle_id)
                        if fresh
                        else None
                    )
                    if fresh is None or fresh.phase != WorkflowPhase.EXECUTING:
                        return fresh or state, False
                    if current is None or current.kind is not AttemptKind.REVIEW_REPAIR:
                        return fresh, False
                    if current.status is AttemptStatus.RUNNING:
                        self.store.recover_orphaned_repair_attempt(
                            current.attempt_id, now=self.clock()
                        )
                        return self.store.workflow_state(state.thread_id), False
                    if current.status is AttemptStatus.SUCCEEDED:
                        self.store.fail_closed_repair_recovery(
                            current.attempt_id, now=self.clock()
                        )
                        return self.store.workflow_state(state.thread_id), False
                    return fresh, False
            except ThreadLockUnavailable:
                return state, True
        return self._recover_initial_execution(state), False

    def next_workflow_input(self, thread_id: str) -> WorkflowInputRef | None:
        state = self.store.workflow_state(thread_id)
        self._drain_approval_controls(thread_id, state)
        if state:
            state, _ = self._recover_executing_state(
                state, lock_root=Path("~/.sweforge/locks").expanduser()
            )
        after = state.root_event_key if state else None
        candidates = list(self.store.deferred_followups(thread_id))
        candidates.extend(
            self.store.unconsumed_inputs(thread_id, after_event_key=after)
        )
        for row in candidates:
            logical_id = (
                row["deferred_id"] if "deferred_id" in row.keys() else row["event_key"]
            )
            if logical_id.startswith("deferred-"):
                return WorkflowInputRef(logical_id, row["event_key"], row["body"])
            if self.store.execution_for_event(row["event_key"]) is None:
                return WorkflowInputRef(logical_id, row["event_key"], row["body"])
        return None

    def next_root(self, thread_id: str) -> str | None:
        """Compatibility wrapper; new scheduling must use next_workflow_input."""
        ref = self.next_workflow_input(thread_id)
        return ref.source_event_key if ref else None

    def begin_next_cycle(self, *, thread_id: str, **plan_kwargs) -> PlanRecord | None:
        state = self.store.workflow_state(thread_id)
        if state and state.phase != WorkflowPhase.IDLE:
            return None
        self._drain_approval_controls(thread_id, state)
        candidates = list(self.store.deferred_followups(thread_id))
        candidates.extend(
            self.store.unconsumed_inputs(
                thread_id, after_event_key=state.root_event_key if state else None
            )
        )
        for row in candidates:
            logical_id = row["deferred_id"] if "deferred_id" in row.keys() else None
            if logical_id or self.store.execution_for_event(row["event_key"]) is None:
                return self.plan_event(
                    event_key=row["event_key"],
                    root_input_id=logical_id,
                    **plan_kwargs,
                )
        return None

    def _run_review_locked(
        self,
        *,
        state: WorkflowStateRecord,
        attempt,
        plan: PlanRecord,
        memory_store: BaseStore | None,
        model: str,
    ) -> ExecutionReviewRecord:
        workspace = self.store.thread_workspace(state.thread_id)
        execution = self.store.execution_for_cycle(
            thread_id=state.thread_id,
            cycle_id=state.cycle_id,
            root_event_key=state.root_event_key,
            root_input_id=state.root_input_id,
        )
        if execution is None or workspace is None:
            raise ValueError("review evidence is unavailable")
        evidence = {
            "plan": {
                "id": plan.plan_id,
                "version": plan.version,
                "text": plan.plan_text,
            },
            "execution": dict(execution),
            "attempt": attempt.__dict__,
            "current_head": execution["end_head_sha"],
            "base_head": workspace.base_commit,
        }
        inspection = Workspace(
            Path(workspace.workspace_path),
            Path(workspace.workspace_path),
            workspace.base_commit,
        )
        evidence["changed_files"] = inspection.changed_files()[:500]
        evidence["diff"] = inspection.diff()[:60_000]
        evidence["dirty"] = not inspection.is_clean()
        source = self.store.source_event(state.root_event_key)
        evidence["source"] = dict(source) if source else {}
        if source:
            evidence["source_request"] = format_source_context(
                source, normalize_task(source["body"])
            )
        if attempt.parent_review_id:
            previous = self.store.execution_review(attempt.parent_review_id)
            evidence["previous_review"] = previous.__dict__ if previous else {}
        review_delivered: set[str] = set()
        result = self.reviewer(
            context=ReviewerContext(
                worktree=workspace.workspace_path,
                repo_context=RepoAgentContext(
                    repo_id=state.repo_id,
                    repo_full_name=state.repo_full_name,
                    thread_id=state.thread_id,
                ),
                memory_store=memory_store,
                memory_namespace=None,
                live_input_provider=None,
                live_delivered_event_keys=review_delivered,
            ),
            model=model,
            evidence=evidence,
        )
        review_id = "review-" + _stable_id(
            attempt.attempt_id, json.dumps(result.model_dump(), sort_keys=True)
        )
        return self.store.save_execution_review(
            ExecutionReviewRecord(
                review_id=review_id,
                thread_id=state.thread_id,
                cycle_id=state.cycle_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                root_event_key=state.root_event_key,
                attempt_id=attempt.attempt_id,
                review_iteration=attempt.attempt_number,
                verdict=result.verdict,
                summary=result.summary,
                findings_json=json.dumps(
                    [item.model_dump() for item in result.findings]
                ),
                repair_instructions_json=json.dumps(result.repair_instructions),
                created_at=self.clock(),
                completed_at=self.clock(),
                requirement_checks_json=json.dumps(
                    [item.model_dump() for item in result.requirement_checks],
                    sort_keys=True,
                ),
                inspection_json=(
                    result.inspection_report.model_dump_json()
                    if result.inspection_report is not None
                    else "{}"
                ),
                challenge_json=(
                    result.semantic_review.model_dump_json()
                    if result.semantic_review is not None
                    else result.challenge_report.model_dump_json()
                    if result.challenge_report is not None
                    else "{}"
                ),
                read_ledger_json=json.dumps(
                    [
                        {key: value for key, value in entry.items() if key != "excerpt"}
                        for entry in result.read_ledger
                    ],
                    sort_keys=True,
                ),
            )
        )

    def advance(
        self,
        *,
        thread_id: str,
        model: str,
        review_model: str | None = None,
        memory_model: str | None = None,
        repo_paths: dict[str, str | Path],
        workspace_root: str | Path,
        memory_store: BaseStore | None = None,
        execute_kwargs: dict | None = None,
        max_review_repairs: int = 5,
    ) -> WorkflowAdvanceResult:
        """Advance one durable worker tick for a single IssueThread.

        The per-thread caller supplies the existing execution arguments. Every
        transition is persisted before the method returns, so a later tick can
        safely recover after a process crash.
        """
        state = self.store.workflow_state(thread_id)
        self._drain_approval_controls(thread_id, state)
        if state:
            was_executing = state.phase == WorkflowPhase.EXECUTING
            recovery_lock_root = (execute_kwargs or {}).get(
                "lock_root", Path("~/.sweforge/locks").expanduser()
            )
            state, busy = self._recover_executing_state(
                state, lock_root=recovery_lock_root
            )
            if busy:
                return WorkflowAdvanceResult(
                    WorkflowPhase.EXECUTING, thread_id, message="busy"
                )
            if was_executing and state.phase == WorkflowPhase.REPAIR_READY:
                return WorkflowAdvanceResult(
                    WorkflowPhase.REPAIR_READY,
                    thread_id,
                    message="orphaned repair recovered",
                )
            state = self.store.workflow_state(thread_id)
            if state and state.phase == WorkflowPhase.WAITING_FOR_INPUT:
                pending_clarification = self.store.clarification_for_thread(thread_id)
                if (
                    pending_clarification
                    and not pending_clarification.response_comment_id
                ):
                    self._persist_clarification(
                        state=state,
                        proposal=ClarificationRequestProposal(
                            question=pending_clarification.question,
                            reason=pending_clarification.reason,
                            answer_type=pending_clarification.answer_type,
                            choices=tuple(
                                json.loads(pending_clarification.choices_json)
                            ),
                            occurrence_key=pending_clarification.occurrence_key,
                        ),
                    )
                self._resolve_open_clarification(state)
                state = self.store.workflow_state(thread_id)
            if state:
                self._defer_active_followups(state)
        if state and state.phase == WorkflowPhase.IDLE:
            learning = self.store.repo_memory_learning(state.root_event_key)
            if learning and learning.status in {
                MemoryLearningStatus.PENDING.value,
                MemoryLearningStatus.FAILED.value,
            }:
                workspace = self.store.thread_workspace(thread_id)
                self._learn_repository_memory(
                    state=state,
                    workspace_path=workspace.workspace_path if workspace else None,
                    memory_store=memory_store,
                    lock_root=(execute_kwargs or {}).get(
                        "memory_lock_root", "~/.sweforge/locks"
                    ),
                    memory_model=memory_model,
                )
                return WorkflowAdvanceResult(
                    WorkflowPhase.IDLE, thread_id, message="repository learning retried"
                )
        if state and state.phase == WorkflowPhase.REVIEW_BLOCKED:
            latest = self.store.latest_attempt(thread_id, state.cycle_id)
            review = (
                self.store.execution_review_for_attempt(latest.attempt_id)
                if latest
                else None
            )
            self.post_blocked_review_comment(
                thread_id,
                summary=review.summary if review else "Execution review is blocked.",
            )
            return WorkflowAdvanceResult(
                WorkflowPhase.REVIEW_BLOCKED, thread_id, message="blocked"
            )
        if state and state.phase == WorkflowPhase.AWAITING_PUBLICATION:
            publication = self.store.publication_for_event(state.root_event_key)
            if publication is None or publication.status.value not in {
                "COMPLETED",
                "NO_CHANGES",
            }:
                return WorkflowAdvanceResult(
                    WorkflowPhase.AWAITING_PUBLICATION, thread_id, message="publishing"
                )
            self.complete_publication(
                thread_id=thread_id,
                publication_status=publication.status.value,
                response_text=(
                    self.store.execution_for_event(state.root_event_key)[
                        "response_text"
                    ]
                    if self.store.execution_for_event(state.root_event_key)
                    else ""
                ),
                pr_url=publication.pr_url,
                memory_store=memory_store,
                memory_lock_root=(execute_kwargs or {}).get(
                    "memory_lock_root", "~/.sweforge/locks"
                ),
                memory_model=memory_model,
            )
            return WorkflowAdvanceResult(
                WorkflowPhase.IDLE, thread_id, message="finalized"
            )
        if state and state.phase == WorkflowPhase.REVIEW_EXECUTION:
            attempt = self.store.latest_attempt(thread_id, state.cycle_id)
            plan = self.store.current_plan(thread_id)
            if attempt is None or plan is None:
                raise ValueError("review workflow is missing its attempt or plan")
            existing_review = self.store.execution_review_for_attempt(
                attempt.attempt_id
            )
            if existing_review:
                if existing_review.verdict == "ACCEPT":
                    self.store.accept_execution_review(
                        existing_review.review_id, now=self.clock()
                    )
                    return WorkflowAdvanceResult(
                        WorkflowPhase.AWAITING_PUBLICATION,
                        thread_id,
                        message="execution accepted",
                    )
                if existing_review.verdict == "NEEDS_FIXES":
                    try:
                        repair = self.store.create_repair_permit(
                            thread_id=thread_id,
                            now=self.clock(),
                            max_repairs=max_review_repairs,
                        )
                    except ValueError as exc:
                        if "maximum" in str(exc):
                            self.store.save_workflow_state(
                                replace(
                                    state,
                                    phase=WorkflowPhase.REVIEW_BLOCKED,
                                    updated_at=self.clock(),
                                )
                            )
                            self.post_blocked_review_comment(
                                thread_id, summary=existing_review.summary
                            )
                            return WorkflowAdvanceResult(
                                WorkflowPhase.REVIEW_BLOCKED,
                                thread_id,
                                message=str(exc),
                            )
                        raise
                    return WorkflowAdvanceResult(
                        WorkflowPhase.REPAIR_READY,
                        thread_id,
                        permit_id=repair.permit_id,
                        message="repair authorized by execution review",
                    )
                if existing_review.verdict == "BLOCKED":
                    try:
                        self.store.block_execution_review(
                            existing_review.review_id, now=self.clock()
                        )
                    except ValueError:
                        raise
                    self.post_blocked_review_comment(
                        thread_id, summary=existing_review.summary
                    )
                    return WorkflowAdvanceResult(
                        WorkflowPhase.REVIEW_BLOCKED,
                        thread_id,
                        message=existing_review.verdict,
                    )
                return WorkflowAdvanceResult(
                    WorkflowPhase.REVIEW_BLOCKED,
                    thread_id,
                    message=existing_review.verdict,
                )
            lock_root = (execute_kwargs or {}).get(
                "lock_root", Path("~/.sweforge/locks").expanduser()
            )
            try:
                with thread_lock(lock_root, thread_id):
                    state = self.store.workflow_state(thread_id)
                    if state is None or state.phase != WorkflowPhase.REVIEW_EXECUTION:
                        return WorkflowAdvanceResult(
                            state.phase if state else WorkflowPhase.IDLE,
                            thread_id,
                            message="review state changed",
                        )
                    attempt = self.store.latest_attempt(thread_id, state.cycle_id)
                    plan = self.store.current_plan(thread_id)
                    if attempt is None or plan is None:
                        raise ValueError(
                            "review workflow is missing its attempt or plan"
                        )
                    review = self.store.execution_review_for_attempt(attempt.attempt_id)
                    if review is None:
                        review = self._run_review_locked(
                            state=state,
                            attempt=attempt,
                            plan=plan,
                            memory_store=memory_store,
                            model=review_model or model,
                        )
            except ThreadLockUnavailable:
                return WorkflowAdvanceResult(
                    WorkflowPhase.REVIEW_EXECUTION, thread_id, message="busy"
                )
            if review.verdict == "ACCEPT":
                self.store.accept_execution_review(review.review_id, now=self.clock())
                return WorkflowAdvanceResult(
                    WorkflowPhase.AWAITING_PUBLICATION,
                    thread_id,
                    message="execution accepted",
                )
            if review.verdict == "NEEDS_FIXES":
                try:
                    repair = self.store.create_repair_permit(
                        thread_id=thread_id,
                        now=self.clock(),
                        max_repairs=max_review_repairs,
                    )
                except ValueError as exc:
                    if "maximum" not in str(exc):
                        raise
                    self.post_blocked_review_comment(thread_id, summary=review.summary)
                    return WorkflowAdvanceResult(
                        WorkflowPhase.REVIEW_BLOCKED, thread_id, message=str(exc)
                    )
                return WorkflowAdvanceResult(
                    WorkflowPhase.REPAIR_READY,
                    thread_id,
                    permit_id=repair.permit_id,
                    message="repair authorized by execution review",
                )
            self.store.block_execution_review(review.review_id, now=self.clock())
            self.post_blocked_review_comment(thread_id, summary=review.summary)
            return WorkflowAdvanceResult(
                WorkflowPhase.REVIEW_BLOCKED, thread_id, message=review.verdict
            )
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

        if state.phase == WorkflowPhase.PLANNING:
            plan = self.store.current_plan(thread_id)
            if plan is None:
                raise ValueError("planning workflow has no current plan")
            if state.planning_feedback_event_key:
                feedback_event = self.store.source_event(
                    state.planning_feedback_event_key
                )
                if feedback_event is None:
                    raise ValueError("revision feedback event is missing")
                plan = self._replan(
                    state=state,
                    feedback=invocation_text(feedback_event["body"])
                    or feedback_event["body"],
                    model=model,
                    repo_paths=repo_paths,
                    workspace_root=workspace_root,
                    memory_store=memory_store,
                    event_key=feedback_event["event_key"],
                )
            if plan.plan_text == "Planning in progress":
                plan = self.plan_event(
                    event_key=state.root_event_key,
                    model=model,
                    repo_paths=repo_paths,
                    workspace_root=workspace_root,
                    memory_store=memory_store,
                )
            if self.client is not None and plan.status is PlanStatus.DRAFT:
                plan = self.publish_plan(plan.plan_id)
            return WorkflowAdvanceResult(
                self.store.workflow_state(thread_id).phase,
                thread_id,
                plan_id=plan.plan_id,
                message="planning resumed",
            )

        pending = self.store.unconsumed_inputs(
            thread_id, after_event_key=state.root_event_key
        )
        all_pending = list(pending)
        plan = self.store.current_plan(thread_id)
        if state.phase == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL and plan is not None:
            pending = self.store.unconsumed_inputs(
                thread_id, after_event_key=state.root_event_key
            )
        if (
            state.phase == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL
            and state.mode == WorkflowMode.AUTO
        ):
            if self.client is not None:
                repo = self.client.repository(state.repo_full_name)
                if not issue_has_auto_label(
                    self.client.issue(repo, state.issue_number)
                ):
                    self.store.save_workflow_state(
                        replace(
                            state,
                            mode=WorkflowMode.INTERACTIVE,
                            updated_at=self.clock(),
                        )
                    )
                elif any(_is_actionable_feedback(row) for row in all_pending):
                    row = next(
                        row for row in all_pending if _is_actionable_feedback(row)
                    )
                    revised = self._replan(
                        state=state,
                        feedback=invocation_text(row["body"]) or row["body"],
                        model=model,
                        repo_paths=repo_paths,
                        workspace_root=workspace_root,
                        memory_store=memory_store,
                        event_key=row["event_key"],
                    )
                    if self.client is not None:
                        revised = self.publish_plan(revised.plan_id)
                    return WorkflowAdvanceResult(
                        self.store.workflow_state(thread_id).phase,
                        thread_id,
                        plan_id=revised.plan_id,
                        message="AUTO plan revised",
                    )
                else:
                    permit = self.authorize_auto(thread_id=thread_id)
                    return WorkflowAdvanceResult(
                        WorkflowPhase.EXECUTION_READY,
                        thread_id,
                        plan_id=permit.plan_id,
                        permit_id=permit.permit_id,
                        message="AUTO plan approved",
                    )
        pending = [
            row for row in pending if matches_current_conversation_target(row, state)
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
            feedback = next(
                (row for row in all_pending if _is_actionable_feedback(row)), None
            )
            if feedback is not None:
                revised = self._replan(
                    state=state,
                    feedback=invocation_text(feedback["body"]) or feedback["body"],
                    model=model,
                    repo_paths=repo_paths,
                    workspace_root=workspace_root,
                    memory_store=memory_store,
                    event_key=feedback["event_key"],
                )
                if self.client is not None:
                    revised = self.publish_plan(revised.plan_id)
                if self.store.workflow_state(thread_id).mode == WorkflowMode.AUTO:
                    permit = self.authorize_auto(thread_id=thread_id)
                    return WorkflowAdvanceResult(
                        WorkflowPhase.EXECUTION_READY,
                        thread_id,
                        plan_id=permit.plan_id,
                        permit_id=permit.permit_id,
                        message="AUTO plan revised",
                    )
                return WorkflowAdvanceResult(
                    self.store.workflow_state(thread_id).phase,
                    thread_id,
                    plan_id=revised.plan_id,
                    message="plan revised",
                )
            permit = self.store.permit_for_plan(state.current_plan_id or "")
            if permit is None:
                raise ValueError("execution-ready workflow has no permit")
            try:
                result = self.execute_authorized(
                    permit_id=permit.permit_id,
                    **(execute_kwargs or {}),
                )
            except PendingWorkflowInputError as exc:
                revised = self._replan(
                    state=state,
                    feedback=invocation_text(
                        self.store.source_event(exc.event_key)["body"]
                    )
                    or self.store.source_event(exc.event_key)["body"],
                    model=model,
                    repo_paths=repo_paths,
                    workspace_root=workspace_root,
                    memory_store=memory_store,
                    event_key=exc.event_key,
                )
                if self.client is not None:
                    revised = self.publish_plan(revised.plan_id)
                return WorkflowAdvanceResult(
                    self.store.workflow_state(thread_id).phase,
                    thread_id,
                    plan_id=revised.plan_id,
                    message="plan revised after execution race",
                )
            return WorkflowAdvanceResult(
                self.store.workflow_state(thread_id).phase,
                thread_id,
                plan_id=permit.plan_id,
                permit_id=permit.permit_id,
                execution=result,
            )
        if state.phase == WorkflowPhase.REPAIR_READY:
            repair = self.store.repair_permit_for_thread(thread_id)
            if repair is None:
                raise ValueError("repair-ready workflow has no repair permit")
            kwargs = dict(execute_kwargs or {})
            result = self.execute_repair_authorized(
                permit_id=repair.permit_id,
                model=kwargs.pop("model", model),
                repo_paths=repo_paths,
                workspace_root=workspace_root,
                lock_root=kwargs.pop("lock_root"),
                checkpointer=kwargs.pop("checkpointer"),
                runner=kwargs.pop("runner", None),
                memory_store=kwargs.pop("memory_store", memory_store),
                capability_registry=kwargs.pop("capability_registry", None),
                sandbox_backend_provider=kwargs.pop("sandbox_backend_provider", None),
                secure_execution=kwargs.pop("secure_execution", True),
                unsafe_local_shell=kwargs.pop("unsafe_local_shell", False),
            )
            return WorkflowAdvanceResult(
                self.store.workflow_state(thread_id).phase,
                thread_id,
                permit_id=repair.permit_id,
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
        current_state = self.store.workflow_state(state.thread_id)
        if not (
            current_state
            and current_state.phase == WorkflowPhase.PLANNING
            and current_state.planning_feedback_event_key == event_key
        ):
            planning_state = self.store.begin_plan_revision(event_key, now=self.clock())
        else:
            planning_state = current_state
        delivered: set[str] = set()

        def live_inputs() -> list[tuple[str, str]]:
            return [
                item
                for item in self.pending_planning_inputs(state.thread_id)
                if item[0] != event_key
            ]

        context = PlannerContext(
            worktree=workspace.workspace_path,
            repo_context=RepoAgentContext(
                repo_id=state.repo_id,
                repo_full_name=state.repo_full_name,
                thread_id=state.thread_id,
            ),
            memory_store=memory_store,
            memory_namespace=None,
            live_input_provider=live_inputs,
            live_delivered_event_keys=delivered,
        )
        plan_text = self.planner(
            context=context,
            model=model,
            task=format_source_context(root, normalize_task(root["body"])),
            feedback=format_source_context(
                self.store.source_event(event_key) or {}, feedback
            ),
        )
        timestamp = self.clock()
        current = self.store.current_plan(state.thread_id)
        if current is None:
            raise ValueError("current plan is missing")
        revised = replace(
            current,
            plan_id="plan-"
            + _stable_id(
                state.thread_id, state.cycle_id, current.version + 1, plan_text
            ),
            version=current.version + 1,
            plan_text=plan_text[:MAX_COMMENT_CHARS],
            status=PlanStatus.DRAFT,
            created_at=timestamp,
            posted_at=None,
            posted_comment_id=None,
            approved_at=None,
            approved_by=None,
            approval_event_key=None,
        )
        result = self.store.finish_plan_revision(
            plan=revised,
            feedback_event_key=event_key,
            finished_at=timestamp,
        )
        for delivered_event_key in delivered:
            self._acknowledge_delivered(
                delivered_event_key,
                thread_id=state.thread_id,
                cycle_id=planning_state.cycle_id,
                purpose=InputPurpose.PLANNING_INPUT,
            )
        return result
