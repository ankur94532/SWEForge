from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from sweforge.agent import build_durable_workflow_agent, build_workflow_agent
from sweforge.agent_trace import AgentTracer
from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import SQLiteGitHubStore
from sweforge.workflow_middleware import (
    DelegatedWorkflowPolicyMiddleware,
    WorkflowAuthority,
    WorkflowPolicyMiddleware,
    WorkflowPolicySnapshot,
    WorkflowSkillsMiddleware,
)
from sweforge.workflow_runtime import TaskPhase, WorkflowCycleKind, WorkflowRuntime
from sweforge.workflow_spec import derive_revision_spec, parse_workflow_spec


class Runtime:
    def __init__(self):
        self.reauthorized = []

    def assert_execution_authorized(self, task_run_id):
        self.reauthorized.append(task_run_id)


class Authority:
    def __init__(
        self,
        phase=TaskPhase.PLANNING,
        tools=("read_file", "glob"),
        skills=(),
    ):
        self.runtime = Runtime()
        self.phase = phase
        self.tools = tools
        self.skills = tuple(skills)

    def snapshot(self):
        if self.phase == TaskPhase.WAITING_FOR_PLAN_APPROVAL:
            raise PermissionError("not runnable")
        return WorkflowPolicySnapshot(
            workflow_id="flow",
            workflow_digest="digest",
            workflow_cycle_id="cycle",
            cycle_id=1,
            active_task_id="A",
            task_run_id="task-run-A",
            phase=self.phase,
            skill=(self.skills[0] if self.skills else f"A-{self.phase.value.lower()}"),
            skills=self.skills,
            configured_tools=frozenset(self.tools),
        )


@dataclass(frozen=True)
class ModelRequest:
    tools: list
    system_message: object | None = None
    model: object | None = None

    def override(self, **values):
        return replace(self, **values)


def tool_request(name, args=None):
    return SimpleNamespace(tool_call={"name": name, "args": args or {}})


def skill_content(name, description, body):
    return (
        f"---\nname: {name}\ndescription: {description}\n---\n\n# Instructions\n{body}"
    )


class TraceSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def test_root_model_sees_only_phase_tools_gateway_and_bounded_task():
    policy = WorkflowPolicyMiddleware(Authority())
    request = ModelRequest(
        tools=[
            SimpleNamespace(name=name)
            for name in (
                "read_file",
                "glob",
                "write_file",
                "execute",
                "submit_plan",
                "finish_execution",
                "task",
            )
        ]
    )
    captured = policy.wrap_model_call(request, lambda item: item)
    assert {tool.name for tool in captured.tools} == {
        "read_file",
        "glob",
        "submit_plan",
        "task",
    }
    assert "active_task=A" in captured.system_message.content


def test_planning_cannot_mutate_even_with_stale_tool_call():
    policy = WorkflowPolicyMiddleware(Authority())
    with pytest.raises(PermissionError, match="forbidden"):
        policy.wrap_tool_call(tool_request("edit_file"), lambda item: "ran")


def test_planning_filters_mutation_even_if_trusted_spec_is_malformed():
    policy = WorkflowPolicyMiddleware(
        Authority(TaskPhase.PLANNING, ("read_file", "edit_file", "execute"))
    )
    request = ModelRequest(
        tools=[
            SimpleNamespace(name=name) for name in ("read_file", "edit_file", "execute")
        ]
    )
    captured = policy.wrap_model_call(request, lambda item: item)
    assert {tool.name for tool in captured.tools} == {"read_file"}
    with pytest.raises(PermissionError, match="forbidden"):
        policy.wrap_tool_call(tool_request("execute"), lambda item: "ran")


def test_mutating_tool_rechecks_exact_permit_at_call_time():
    authority = Authority(
        TaskPhase.EXECUTING, ("read_file", "write_file", "edit_file", "execute")
    )
    policy = WorkflowPolicyMiddleware(authority)
    assert policy.wrap_tool_call(tool_request("edit_file"), lambda item: "ran") == "ran"
    assert authority.runtime.reauthorized == ["task-run-A"]


def test_custom_execution_tool_rechecks_exact_permit_at_call_time():
    authority = Authority(TaskPhase.EXECUTING, ("trusted_server_change",))
    policy = WorkflowPolicyMiddleware(authority)
    assert (
        policy.wrap_tool_call(tool_request("trusted_server_change"), lambda item: "ran")
        == "ran"
    )
    assert authority.runtime.reauthorized == ["task-run-A"]


def test_inactive_task_skill_is_rejected():
    policy = WorkflowPolicyMiddleware(Authority())
    with pytest.raises(PermissionError, match="inactive"):
        policy.wrap_tool_call(
            tool_request("read_file", {"file_path": "/skills/B-planning/SKILL.md"}),
            lambda item: "leaked",
        )


@pytest.mark.parametrize(
    "path",
    [
        "/skills/A-planning/../B-planning/SKILL.md",
        "/skills/A-planning//SKILL.md",
        "/skills/A-planning/./SKILL.md",
        "/skills/A-planning/%2e%2e/SKILL.md",
        "/skills/A-planning\\SKILL.md",
    ],
)
def test_alternate_or_traversal_skill_paths_are_rejected(path):
    policy = WorkflowPolicyMiddleware(Authority())
    with pytest.raises(PermissionError, match="canonical"):
        policy.wrap_tool_call(
            tool_request("read_file", {"file_path": path}),
            lambda item: "leaked",
        )


@pytest.mark.parametrize(
    "name",
    [
        "submit_plan",
        "finish_execution",
        "finish_validation",
        "edit_file",
        "execute",
        "task",
    ],
)
def test_investigator_cannot_call_lifecycle_mutation_or_delegate(name):
    delegated = DelegatedWorkflowPolicyMiddleware(Authority())
    with pytest.raises(PermissionError, match="forbidden"):
        delegated.wrap_tool_call(tool_request(name), lambda item: "ran")


def test_investigator_can_use_read_only_tool_in_active_phase():
    delegated = DelegatedWorkflowPolicyMiddleware(Authority())
    assert (
        delegated.wrap_tool_call(
            tool_request("read_file", {"file_path": "src/example.py"}),
            lambda item: "evidence",
        )
        == "evidence"
    )


def test_investigator_cannot_read_an_inactive_skill():
    delegated = DelegatedWorkflowPolicyMiddleware(
        Authority(skills=("active", "also-active"))
    )
    with pytest.raises(PermissionError, match="inactive"):
        delegated.wrap_tool_call(
            tool_request("read_file", {"file_path": "/skills/other/SKILL.md"}),
            lambda item: "leaked",
        )


def test_waiting_for_approval_runs_no_root_or_subagent_model():
    authority = Authority(TaskPhase.WAITING_FOR_PLAN_APPROVAL)
    request = ModelRequest(tools=[])
    with pytest.raises(PermissionError, match="not runnable"):
        WorkflowPolicyMiddleware(authority).wrap_model_call(request, lambda item: item)
    with pytest.raises(PermissionError, match="not runnable"):
        DelegatedWorkflowPolicyMiddleware(authority).wrap_model_call(
            request, lambda item: item
        )


def test_skill_middleware_discloses_only_active_skill():
    seen = []

    def read_skill(cycle_id, skill):
        seen.append((cycle_id, skill))
        return "Only A planning instructions"

    request = ModelRequest(tools=[])
    result = WorkflowSkillsMiddleware(Authority(), read_skill).wrap_model_call(
        request, lambda item: item
    )
    assert seen == [(1, "A-planning")]
    assert "Only A planning" in result.system_message.content
    assert "B-" not in result.system_message.content


def test_multiple_skills_disclose_ordered_metadata_without_bodies_or_new_tools():
    contents = {
        "model": skill_content("model", "Reason about models.", "MODEL BODY"),
        "reporting": skill_content(
            "reporting", "Prepare compatible reports.", "REPORTING BODY"
        ),
    }
    seen = []

    def read_skill(cycle_id, skill):
        seen.append((cycle_id, skill))
        return contents[skill]

    authority = Authority(skills=("model", "reporting"))
    tools = [SimpleNamespace(name="read_file"), SimpleNamespace(name="glob")]
    request = ModelRequest(tools=tools)
    result = WorkflowSkillsMiddleware(authority, read_skill).wrap_model_call(
        request, lambda item: item
    )
    prompt = result.system_message.content

    assert seen == [(1, "model"), (1, "reporting")]
    assert prompt.index("- model:") < prompt.index("- reporting:")
    assert "Reason about models." in prompt
    assert "Prepare compatible reports." in prompt
    assert "/skills/model/SKILL.md" in prompt
    assert "/skills/reporting/SKILL.md" in prompt
    assert "MODEL BODY" not in prompt
    assert "REPORTING BODY" not in prompt
    assert result.tools == tools


def test_four_skill_revision_catalog_excludes_all_full_bodies():
    skills = ("model", "config", "readiness", "reporting")
    contents = {
        name: skill_content(name, f"Select {name} guidance.", f"SECRET {name} BODY")
        for name in skills
    }
    authority = Authority(skills=skills)
    result = WorkflowSkillsMiddleware(
        authority, lambda _cycle_id, skill: contents[skill]
    ).wrap_model_call(ModelRequest(tools=[]), lambda item: item)

    for name in skills:
        assert f"- {name}: Select {name} guidance." in result.system_message.content
        assert f"/skills/{name}/SKILL.md" in result.system_message.content
        assert f"SECRET {name} BODY" not in result.system_message.content


def test_multiple_skills_fail_closed_without_phase_authorized_read_file():
    authority = Authority(tools=("glob",), skills=("model", "reporting"))
    with pytest.raises(PermissionError, match="requires phase-authorized read_file"):
        WorkflowSkillsMiddleware(
            authority, lambda _cycle_id, _skill: "unused"
        ).wrap_model_call(ModelRequest(tools=[]), lambda item: item)


def test_invalid_multi_skill_metadata_is_a_permission_failure_at_boundary():
    authority = Authority(skills=("model", "reporting"))

    def read_skill(_cycle_id, skill):
        if skill == "model":
            return skill_content("model", "Use models.", "MODEL BODY")
        return "---\nname: other\ndescription: mismatch\n---\nREPORT BODY"

    with pytest.raises(PermissionError, match="metadata is invalid"):
        WorkflowSkillsMiddleware(authority, read_skill).wrap_model_call(
            ModelRequest(tools=[]), lambda item: item
        )


def test_duplicate_active_skill_metadata_fails_closed():
    authority = Authority(skills=("model", "model"))
    content = skill_content("model", "Use models.", "MODEL BODY")
    with pytest.raises(PermissionError, match="duplicate"):
        WorkflowSkillsMiddleware(
            authority, lambda _cycle_id, _skill: content
        ).wrap_model_call(ModelRequest(tools=[]), lambda item: item)


def test_catalog_rebuild_is_deterministic_without_application_skill_state():
    authority = Authority(skills=("model", "reporting"))
    contents = {
        name: skill_content(name, f"Use {name}.", f"PRIVATE {name} BODY")
        for name in authority.skills
    }
    middleware = WorkflowSkillsMiddleware(
        authority, lambda _cycle_id, skill: contents[skill]
    )

    first = middleware.wrap_model_call(ModelRequest(tools=[]), lambda item: item)
    second = middleware.wrap_model_call(ModelRequest(tools=[]), lambda item: item)

    assert first.system_message.content == second.system_message.content
    assert not hasattr(middleware, "loaded_skills")


def test_authorized_skill_read_is_selective_and_does_not_change_authority():
    authority = Authority(skills=("model", "reporting"))
    policy = WorkflowPolicyMiddleware(authority)
    before = authority.snapshot()
    calls = []

    result = policy.wrap_tool_call(
        tool_request("read_file", {"file_path": "/skills/reporting/SKILL.md"}),
        lambda request: (
            calls.append(request.tool_call["args"]["file_path"]) or "REPORTING BODY"
        ),
    )

    assert result == "REPORTING BODY"
    assert calls == ["/skills/reporting/SKILL.md"]
    assert authority.snapshot() == before
    assert authority.runtime.reauthorized == []


def test_authorized_skill_supporting_file_is_readable_but_not_traced():
    sink = TraceSink()
    policy = WorkflowPolicyMiddleware(
        Authority(skills=("model", "reporting")), tracer=AgentTracer(sink)
    )

    assert (
        policy.wrap_tool_call(
            tool_request(
                "read_file", {"file_path": "/skills/reporting/references/rules.md"}
            ),
            lambda _request: "rules",
        )
        == "rules"
    )
    assert sink.events == []


def test_skill_catalog_and_successful_canonical_read_trace_metadata_only():
    sink = TraceSink()
    tracer = AgentTracer(sink)
    authority = Authority(skills=("model", "reporting"))
    contents = {
        name: skill_content(name, f"Use {name}.", f"PRIVATE {name} BODY")
        for name in authority.skills
    }
    WorkflowSkillsMiddleware(
        authority,
        lambda _cycle_id, skill: contents[skill],
        tracer=tracer,
    ).wrap_model_call(ModelRequest(tools=[]), lambda item: item)
    WorkflowPolicyMiddleware(authority, tracer=tracer).wrap_tool_call(
        tool_request("read_file", {"file_path": "/skills/reporting/SKILL.md"}),
        lambda _request: contents["reporting"],
    )

    assert [(event.category, event.message) for event in sink.events] == [
        ("SKILL CATALOG", "count=2"),
        ("SKILL READ", "skill=reporting"),
    ]
    assert all("PRIVATE" not in event.message for event in sink.events)
    assert sink.events[1].context.task_id == "A"
    assert sink.events[1].context.phase == "PLANNING"


def test_failed_skill_read_is_not_traced_and_tracing_off_is_silent():
    sink = TraceSink()
    authority = Authority(skills=("model", "reporting"))
    policy = WorkflowPolicyMiddleware(authority, tracer=AgentTracer(sink))

    with pytest.raises(RuntimeError, match="backend failure"):
        policy.wrap_tool_call(
            tool_request("read_file", {"file_path": "/skills/model/SKILL.md"}),
            lambda _request: (_ for _ in ()).throw(RuntimeError("backend failure")),
        )
    assert sink.events == []
    assert (
        WorkflowPolicyMiddleware(authority).wrap_tool_call(
            tool_request("read_file", {"file_path": "/skills/model/SKILL.md"}),
            lambda _request: "MODEL BODY",
        )
        == "MODEL BODY"
    )


def test_validation_cannot_mutate_after_skill_discovery():
    authority = Authority(
        TaskPhase.VALIDATING,
        ("read_file", "edit_file", "execute"),
        ("model", "reporting"),
    )
    policy = WorkflowPolicyMiddleware(authority)
    with pytest.raises(PermissionError, match="forbidden"):
        policy.wrap_tool_call(tool_request("edit_file"), lambda item: "ran")


@pytest.mark.parametrize("phase", [TaskPhase.PLANNING, TaskPhase.VALIDATING])
def test_registered_mutating_script_is_denied_outside_execution(phase):
    authority = Authority(phase, ("release_deploy",))
    policy = WorkflowPolicyMiddleware(
        authority, tool_effects={"release_deploy": "mutate"}
    )
    request = ModelRequest(tools=[SimpleNamespace(name="release_deploy")])
    captured = policy.wrap_model_call(request, lambda item: item)
    assert captured.tools == []
    with pytest.raises(PermissionError, match="forbidden"):
        policy.wrap_tool_call(tool_request("release_deploy"), lambda _item: "ran")


def test_registered_mutating_script_requires_execution_workflow_authority():
    authority = Authority(TaskPhase.EXECUTING, ("release_deploy",))
    policy = WorkflowPolicyMiddleware(
        authority, tool_effects={"release_deploy": "mutate"}
    )
    assert (
        policy.wrap_tool_call(tool_request("release_deploy"), lambda _item: "ran")
        == "ran"
    )
    assert authority.runtime.reauthorized == ["task-run-A"]


def test_investigator_gets_only_registered_read_effect_tools():
    authority = Authority(TaskPhase.EXECUTING, ("read_release", "release_deploy"))
    policy = DelegatedWorkflowPolicyMiddleware(
        authority,
        tool_effects={"read_release": "read", "release_deploy": "mutate"},
    )
    request = ModelRequest(
        tools=[
            SimpleNamespace(name="read_release"),
            SimpleNamespace(name="release_deploy"),
        ]
    )
    captured = policy.wrap_model_call(request, lambda item: item)
    assert [tool.name for tool in captured.tools] == ["read_release"]


def test_canonical_builder_requires_explicit_bounded_default_override(monkeypatch):
    with pytest.raises(ValueError, match="explicit bounded"):
        build_workflow_agent(
            model="provider:model",
            tools=[],
            backend=object(),
            system_prompt="test",
            subagents=[],
        )

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return "compiled"

    monkeypatch.setattr("sweforge.agent.create_deep_agent", fake_create)
    result = build_workflow_agent(
        model="provider:model",
        tools=[],
        backend=object(),
        system_prompt="test",
        subagents=[
            {
                "name": "general-purpose",
                "description": "read only",
                "system_prompt": "inspect",
                "tools": [],
            }
        ],
    )
    assert result == "compiled"
    assert captured["name"] == "sweforge-workflow-root"
    assert captured["subagents"][0]["name"] == "general-purpose"


def test_durable_builder_keeps_gateways_root_only(monkeypatch):
    captured = {}

    def fake_builder(**kwargs):
        captured.update(kwargs)
        return "root"

    monkeypatch.setattr("sweforge.agent.build_workflow_agent", fake_builder)
    monkeypatch.setattr(
        "sweforge.agent.WorkflowPolicyMiddleware",
        lambda authority, phase_models, **kwargs: SimpleNamespace(
            authority=authority, phase_models=phase_models, **kwargs
        ),
    )
    gateway = SimpleNamespace(name="submit_plan")
    tracer = object()
    result = build_durable_workflow_agent(
        planning_model="openai:test",
        execution_model="openai:test",
        validation_model="openai:test",
        backend=object(),
        authority=Authority(),
        lifecycle_tools=[gateway],
        read_skill=lambda cycle, skill: "instructions",
        checkpointer=object(),
        tracer=tracer,
    )
    assert result == "root"
    assert gateway in captured["tools"]
    investigator = captured["subagents"][0]
    assert gateway not in investigator["tools"]
    assert investigator["name"] == "general-purpose"
    assert captured["middleware"][0].tracer is tracer
    assert captured["middleware"][1].tracer is tracer
    assert investigator["middleware"][0].tracer is tracer
    assert investigator["middleware"][1].tracer is tracer


@pytest.mark.parametrize(
    ("phase", "kind", "gateway"),
    [
        (TaskPhase.PLANNING, "PLAN", "submit_plan"),
        (TaskPhase.VALIDATING, "RESULT", "finish_validation"),
    ],
)
def test_reviewing_feedback_still_authorizes_its_replayed_gateway(phase, kind, gateway):
    """A resumed feedback review replays the interrupted lifecycle gateway.

    ``submit_plan``/``finish_validation`` raised the approval interrupt, so
    LangGraph re-executes them with the PLAN_FEEDBACK/RESULT_FEEDBACK resume
    payload; the gateway then returns the review instruction rather than
    approving anything. Excluding it from the REVIEWING tool set makes every
    feedback resume fail closed and the review can never be decided.
    """
    snapshot = WorkflowPolicySnapshot(
        workflow_id="flow",
        workflow_digest="digest",
        workflow_cycle_id="cycle",
        cycle_id=1,
        active_task_id="A",
        task_run_id="task-run-A",
        phase=phase,
        skill="A-skill",
        skills=(),
        configured_tools=frozenset({"read_file", "glob", "edit_file"}),
        feedback_review_id="feedback-review-1",
        feedback_review_kind=kind,
        feedback_review_status="REVIEWING",
    )

    allowed = WorkflowPolicyMiddleware.allowed_tools(snapshot, {})

    assert gateway in allowed
    assert {"replan_current_feedback", "defer_current_feedback_to_revision"} <= allowed
    # The review stays read-only: no mutation escapes through this branch.
    assert "edit_file" not in allowed
    assert "finish_execution" not in allowed


# --- phase-wise revision skill authority ----------------------------------


def _phase_skill_spec():
    """Original workflow whose three phases carry visibly distinct skills."""
    return parse_workflow_spec(
        {
            "version": 1,
            "workflow_id": "phase-skills",
            "tasks": [
                {
                    "id": task_id,
                    "depends_on": depends,
                    "planning": {
                        "skill": f"plan-{task_id}",
                        "tools": ["read_file", "glob"],
                    },
                    "execution": {
                        "skill": f"execute-{task_id}",
                        "tools": ["read_file", "edit_file", "execute"],
                    },
                    "validation": {
                        "skill": f"validate-{task_id}",
                        "tools": ["read_file", "run_validation"],
                    },
                }
                for task_id, depends in (("api", []), ("db", ["api"]))
            ],
        }
    )


@pytest.fixture
def revision_authority(tmp_path):
    """A real revision cycle over the derived phase-wise envelope."""
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(321, "example/repo")
    event = SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE_COMMENT,
        source_id="1",
        source_updated_at="2026-01-01T00:00:00Z",
        source_created_at="2026-01-01T00:00:00Z",
        subject_kind=SubjectKind.ISSUE,
        subject_number=9,
        author_login="owner",
        body="@agent revise",
        html_url=None,
    )
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.record_batch(
        repo.repo_id, "issue_comments", [event], since="now", etag=None, polled_at="now"
    )
    thread_id = store.source_event(event.event_key)["thread_id"]
    engine = WorkflowRuntime(store, clock=lambda: "2026-01-01T00:10:00Z")
    revision_spec = derive_revision_spec(_phase_skill_spec())
    cycle = engine.initialize_cycle(
        thread_id=thread_id,
        cycle_id=1,
        root_input_id=event.event_key,
        spec=revision_spec,
        spec_ref="derived",
        cycle_kind=WorkflowCycleKind.REVISION,
        revision_sequence=1,
    )
    task = engine.select_active_task(cycle.workflow_cycle_id)
    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, revision_spec)
    yield engine, authority, task
    store.close()


def _advance_to_executing(engine, task):
    plan = engine.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="revision plan",
        posted_comment_id=1,
        posted_at="2026-01-01T00:11:00Z",
    )
    engine.approve_plan(
        task_run_id=task.task_run_id,
        occurrence_key=plan.approval_occurrence_key,
        approval_event_key="approval",
        approved_by="maintainer",
        approval_is_authorized=True,
        approval_occurred_at="2026-01-01T00:12:00Z",
    )


def _advance_to_validating(engine, task):
    engine.finish_execution(
        task.task_run_id,
        evidence={
            "reported": {"done": True},
            "tool_observations": [{"command": "t", "exit_code": 0, "output": "ok"}],
        },
    )


PLANNING_SKILLS = ("plan-api", "plan-db")
EXECUTION_SKILLS = ("execute-api", "execute-db")
VALIDATION_SKILLS = ("validate-api", "validate-db")


def test_revision_snapshot_exposes_exactly_the_derived_phase_skills(
    revision_authority,
):
    engine, authority, task = revision_authority

    assert authority.snapshot().skills == PLANNING_SKILLS
    _advance_to_executing(engine, task)
    assert authority.snapshot().skills == EXECUTION_SKILLS
    _advance_to_validating(engine, task)
    assert authority.snapshot().skills == VALIDATION_SKILLS


def test_revision_skill_catalog_advertises_only_the_current_phase(revision_authority):
    engine, authority, task = revision_authority

    def catalog():
        result = WorkflowSkillsMiddleware(
            authority,
            lambda _cycle_id, name: skill_content(
                name, f"Guidance for {name}.", f"BODY {name}"
            ),
        ).wrap_model_call(
            ModelRequest(tools=[SimpleNamespace(name="read_file")]), lambda item: item
        )
        return result.system_message.content

    for advance, present, absent in (
        (None, PLANNING_SKILLS, EXECUTION_SKILLS + VALIDATION_SKILLS),
        (_advance_to_executing, EXECUTION_SKILLS, PLANNING_SKILLS + VALIDATION_SKILLS),
        (_advance_to_validating, VALIDATION_SKILLS, PLANNING_SKILLS + EXECUTION_SKILLS),
    ):
        if advance is not None:
            advance(engine, task)
        prompt = catalog()
        for name in present:
            assert f"- {name}: Guidance for {name}." in prompt
            assert f"/skills/{name}/SKILL.md" in prompt
            assert f"BODY {name}" not in prompt
        for name in absent:
            assert name not in prompt


def test_reading_another_revision_phase_skill_is_rejected(revision_authority):
    engine, authority, task = revision_authority
    policy = WorkflowPolicyMiddleware(authority)

    # Planning may read its own skills, never an execution- or validation-only one.
    assert (
        policy.wrap_tool_call(
            tool_request("read_file", {"file_path": "/skills/plan-api/SKILL.md"}),
            lambda _item: "ok",
        )
        == "ok"
    )
    for name in EXECUTION_SKILLS + VALIDATION_SKILLS:
        with pytest.raises(PermissionError, match="inactive task/phase skill"):
            policy.wrap_tool_call(
                tool_request("read_file", {"file_path": f"/skills/{name}/SKILL.md"}),
                lambda _item: "ok",
            )

    _advance_to_executing(engine, task)
    assert (
        policy.wrap_tool_call(
            tool_request("read_file", {"file_path": "/skills/execute-db/SKILL.md"}),
            lambda _item: "ok",
        )
        == "ok"
    )
    for name in PLANNING_SKILLS + VALIDATION_SKILLS:
        with pytest.raises(PermissionError, match="inactive task/phase skill"):
            policy.wrap_tool_call(
                tool_request("read_file", {"file_path": f"/skills/{name}/SKILL.md"}),
                lambda _item: "ok",
            )


def test_single_skill_revision_phase_still_eagerly_loads_its_body(tmp_path):
    """A one-skill phase keeps the eager-load path rather than a catalog."""
    single = parse_workflow_spec(
        {
            "version": 1,
            "workflow_id": "single",
            "tasks": [
                {
                    "id": "only",
                    "depends_on": [],
                    "planning": {"skill": "plan-only", "tools": ["read_file"]},
                    "execution": {"skill": "execute-only", "tools": ["read_file"]},
                    "validation": {
                        "skill": "validate-only",
                        "tools": ["read_file", "run_validation"],
                    },
                }
            ],
        }
    )
    revision = derive_revision_spec(single)
    assert revision.tasks[0].planning.skills == ("plan-only",)

    authority = Authority(skills=("plan-only",))
    result = WorkflowSkillsMiddleware(
        authority, lambda _cycle_id, name: f"FULL {name} BODY"
    ).wrap_model_call(ModelRequest(tools=[]), lambda item: item)
    assert "[Trusted skill: plan-only]" in result.system_message.content
    assert "FULL plan-only BODY" in result.system_message.content
