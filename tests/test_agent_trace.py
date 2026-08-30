import io
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langgraph.types import Command

from sweforge.agent_trace import (
    AgentTraceCallbackHandler,
    AgentTracer,
    TerminalTraceSink,
    TraceContext,
    redact_text,
)
from sweforge.github_models import InteractionMode
from sweforge.server import ServerConfig, SWEForgeServer
from sweforge.server_cli import build_parser
from sweforge.workflow_agent_runtime import invoke_workflow_phase
from sweforge.workflow_runtime import TaskPhase
from sweforge.workflow_tools import build_lifecycle_tools


class CaptureSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _context(phase="PLANNING", *, thread="github:1:issue:12", task="A"):
    return TraceContext(
        thread_id=thread,
        repo="owner/repo",
        issue_number=12,
        workflow_cycle_id="workflow-cycle-2",
        cycle_id=2,
        task_id=task,
        task_run_id=f"task-run-{task}",
        phase=phase,
    )


def _config(tmp_path, **updates):
    values = {
        "repositories": ("owner/repo",),
        "repo_paths": {"owner/repo": tmp_path},
        "db": tmp_path / "state.db",
        "checkpoints": tmp_path / "checkpoints.sqlite",
        "memory_db": tmp_path / "memory.sqlite",
        "workspace_root": tmp_path / "workspaces",
        "lock_root": tmp_path / "locks",
        "model": "openai:test-model",
    }
    values.update(updates)
    return ServerConfig(**values)


def test_debug_is_off_by_default_and_cli_flags_are_opt_in(tmp_path):
    sink = CaptureSink()
    server = SWEForgeServer(
        _config(tmp_path),
        client_factory=lambda _config: (None, None),
        trace_sink=sink,
    )

    assert server.tracer is None
    assert sink.events == []
    enabled_server = SWEForgeServer(
        _config(tmp_path, debug_agent=True),
        client_factory=lambda _config: (None, None),
        trace_sink=sink,
    )
    assert enabled_server.tracer is not None
    defaults = build_parser().parse_args(
        ["--repo-path", f"owner/repo={tmp_path}", "--model", "test-model"]
    )
    assert defaults.debug_agent is False
    assert defaults.debug_agent_tools is False
    enabled = build_parser().parse_args(
        [
            "--repo-path",
            f"owner/repo={tmp_path}",
            "--model",
            "test-model",
            "--debug-agent",
            "--debug-agent-tools",
        ]
    )
    assert enabled.debug_agent is True
    assert enabled.debug_agent_tools is True


def test_model_callback_emits_only_new_visible_text_once_and_model_identity():
    sink = CaptureSink()
    tracer = AgentTracer(sink)
    handler = AgentTraceCallbackHandler(
        tracer,
        context_provider=_context,
        model_names={"planning": "openai:gpt-test"},
    )
    run_id = uuid4()
    handler.on_chat_model_start({}, [[]], run_id=run_id)
    message = AIMessage(
        content=[
            {"type": "reasoning", "text": "private reasoning must stay hidden"},
            {"type": "text", "text": "I will inspect the configuration."},
        ]
    )
    generation = ChatGeneration(message=message)
    handler.on_llm_end(LLMResult(generations=[[generation, generation]]), run_id=run_id)

    rendered = "\n".join(f"{item.category}: {item.message}" for item in sink.events)
    assert "MODEL START: planning_model=openai:gpt-test" in rendered
    assert rendered.count("I will inspect the configuration.") == 1
    assert "private reasoning" not in rendered
    assert "MODEL END: completed" in rendered
    assert sink.events[0].context.model == "openai:gpt-test"


def test_tool_and_subagent_events_are_bounded_and_redacted(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-super-secret")
    sink = CaptureSink()
    tracer = AgentTracer(sink, include_tool_payloads=True)
    handler = AgentTraceCallbackHandler(tracer, context_provider=_context)
    tool_run = uuid4()
    handler.on_tool_start(
        {"name": "execute"},
        "",
        run_id=tool_run,
        inputs={
            "command": "pytest",
            "authorization": "Bearer sk-test-super-secret",
        },
    )
    handler.on_tool_end("x" * 10_000 + " sk-test-super-secret", run_id=tool_run)
    subagent_run = uuid4()
    handler.on_tool_start({"name": "task"}, "investigate", run_id=subagent_run)
    handler.on_tool_end("done", run_id=subagent_run)
    lifecycle_run = uuid4()
    handler.on_tool_start({"name": "submit_plan"}, "plan", run_id=lifecycle_run)
    handler.on_tool_end("waiting", run_id=lifecycle_run)

    rendered = "\n".join(f"{item.category}: {item.message}" for item in sink.events)
    assert "TOOL START: execute" in rendered
    assert "TOOL END: execute" in rendered
    assert "SUBAGENT START: task" in rendered
    assert "SUBAGENT END: task" in rendered
    assert "LIFECYCLE START: submit_plan" in rendered
    assert "LIFECYCLE END: submit_plan" in rendered
    assert "[truncated " in rendered
    result = next(item for item in sink.events if item.category == "TOOL RESULT")
    assert len(result.message) <= 4_000
    assert "sk-test-super-secret" not in rendered
    assert "[REDACTED]" in rendered


def test_common_redaction_covers_headers_urls_private_keys_and_known_env(monkeypatch):
    monkeypatch.setenv("SWEFORGE_GITHUB_TOKEN", "github_pat_knownsecret")
    value = """Authorization: Bearer abc.def
https://alice:password@example.test/repo.git
github_pat_knownsecret
-----BEGIN RSA PRIVATE KEY-----
private-material
-----END RSA PRIVATE KEY-----"""

    rendered = redact_text(value)

    assert "abc.def" not in rendered
    assert "alice" not in rendered
    assert "password" not in rendered
    assert "github_pat_knownsecret" not in rendered
    assert "private-material" not in rendered
    assert "[REDACTED]" in rendered


def test_terminal_sink_prefixes_every_line_and_keeps_concurrent_events_atomic():
    stream = io.StringIO()
    sink = TerminalTraceSink(stream)
    tracers = [AgentTracer(sink), AgentTracer(sink)]

    def emit(index):
        task = f"T{index % 2}"
        tracers[index % 2].emit(
            "MODEL", f"line-{index}-a\nline-{index}-b", _context(task=task)
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(emit, range(100)))

    lines = stream.getvalue().splitlines()
    assert len(lines) == 200
    assert all(line.startswith("[thread=github:1:issue:12]") for line in lines)
    for index in range(100):
        first = next(i for i, line in enumerate(lines) if f"line-{index}-a" in line)
        assert f"line-{index}-b" in lines[first + 1]
        assert f"[task=T{index % 2}]" in lines[first]


def test_trace_sink_failure_never_changes_execution():
    class BrokenSink:
        def emit(self, _event):
            raise OSError("terminal closed")

    AgentTracer(BrokenSink()).emit("MODEL", "still observational", _context())


class FakeLifecycleRuntime:
    def __init__(self, mode=InteractionMode.AUTO):
        self.store = SimpleNamespace(interaction_mode=lambda _thread_id: mode)
        self.phase = TaskPhase.PLANNING
        self.calls = []

    def _task(self):
        return SimpleNamespace(
            task_run_id="task-run-A",
            task_id="A",
            thread_id="github:1:issue:12",
            workflow_cycle_id="workflow-cycle-2",
            cycle_id=2,
            phase=self.phase,
            current_plan_id="plan-A" if self.phase != TaskPhase.PLANNING else None,
            clarification_occurrence_key=None,
        )

    def active_task(self, _cycle):
        return self._task()

    def task(self, _task_run_id):
        return self._task()

    def submit_posted_plan(self, **_kwargs):
        self.calls.append("submit_plan")
        self.phase = TaskPhase.WAITING_FOR_PLAN_APPROVAL
        return SimpleNamespace(
            plan_id="plan-A",
            approval_occurrence_key="plan-approval-A",
            version=1,
            plan_digest="plan-digest-A",
        )

    def auto_authorize_plan(self, _task_run_id):
        self.calls.append("auto_plan")
        self.phase = TaskPhase.EXECUTING

    def approve_plan(self, **_kwargs):
        self.calls.append("human_plan")
        self.phase = TaskPhase.EXECUTING

    def finish_execution(self, _task_run_id, **_kwargs):
        self.calls.append("finish_execution")
        self.phase = TaskPhase.VALIDATING

    def finish_validation(self, **_kwargs):
        self.calls.append("finish_validation")
        return self._task()

    def publish_validated_result(self, **_kwargs):
        self.calls.append("publish_result")
        self.phase = TaskPhase.WAITING_FOR_RESULT_APPROVAL
        return self.current_result("task-run-A")

    def current_result(self, _task_run_id):
        return SimpleNamespace(
            result_occurrence_key="result-approval-A",
            plan_id="plan-A",
            execution_id="execution-A",
            validation_id="validation-A",
            result_id="result-A",
        )

    def auto_accept_result(self, _task_run_id):
        self.calls.append("auto_result")
        self.phase = TaskPhase.DONE

    def approve_result(self, **_kwargs):
        self.calls.append("human_result")
        self.phase = TaskPhase.DONE


def _run_accepted_lifecycle(runtime, tracer=None):
    tools = {
        item.name: item
        for item in build_lifecycle_tools(
            runtime=runtime,
            workflow_cycle_id="workflow-cycle-2",
            publish_plan=lambda **_kwargs: (101, "2026-01-01T00:00:00Z"),
            publish_result=lambda **_kwargs: (102, "2026-01-01T00:01:00Z"),
            tracer=tracer,
            trace_context=lambda task: _context(task=task.task_id).with_values(
                phase=task.phase.value
            ),
        )
    }
    tools["submit_plan"].invoke({"plan_text": "inspect, implement, test"})
    tools["finish_execution"].invoke(
        {"summary": "implemented", "evidence": {"tests": "passed"}}
    )
    tools["finish_validation"].invoke(
        {
            "verdict": "ACCEPT",
            "summary": "valid",
            "findings": [],
            "repair_instructions": [],
            "evidence": {"pytest": "passed"},
        }
    )
    return runtime.phase, runtime.calls


def test_lifecycle_tracing_uses_authoritative_transitions_and_is_observational():
    untraced = FakeLifecycleRuntime()
    traced = FakeLifecycleRuntime()
    sink = CaptureSink()

    untraced_result = _run_accepted_lifecycle(untraced)
    traced_result = _run_accepted_lifecycle(traced, AgentTracer(sink))

    assert untraced_result == traced_result
    assert traced.phase == TaskPhase.DONE
    rendered = "\n".join(f"{item.category}: {item.message}" for item in sink.events)
    assert "PLANNING -> WAITING_FOR_PLAN_APPROVAL" in rendered
    assert "WAITING_FOR_PLAN_APPROVAL -> EXECUTING" in rendered
    assert "EXECUTING -> VALIDATING" in rendered
    assert "VALIDATING -> WAITING_FOR_RESULT_APPROVAL" in rendered
    assert "WAITING_FOR_RESULT_APPROVAL -> DONE" in rendered
    assert "VALIDATION: ACCEPT" in rendered
    assert "AUTHORIZATION: AUTO plan" in rendered
    assert "AUTHORIZATION: AUTO result" in rendered


def test_human_authority_events_are_emitted_from_gateway_results(monkeypatch):
    def approve(payload):
        return {
            "kind": payload["kind"],
            "occurrence_key": payload["occurrence_key"],
            "event_key": "github-event",
            "approved_by": "maintainer",
            "approved_at": "2026-01-01T00:02:00Z",
            "authorized": True,
        }

    monkeypatch.setattr("sweforge.workflow_tools.interrupt", approve)
    sink = CaptureSink()
    runtime = FakeLifecycleRuntime(mode=InteractionMode.MANUAL)

    _run_accepted_lifecycle(runtime, AgentTracer(sink))

    rendered = "\n".join(item.message for item in sink.events)
    assert "HUMAN plan by maintainer" in rendered
    assert "HUMAN result by maintainer" in rendered
    assert runtime.phase == TaskPhase.DONE


class FakeAuthority:
    def __init__(self):
        self.before = SimpleNamespace(
            workflow_cycle_id="workflow-cycle-2",
            task_run_id="task-run-A",
            phase=TaskPhase.PLANNING,
        )
        self.active = SimpleNamespace(
            task_run_id="task-run-A",
            phase=TaskPhase.PLANNING,
            waiting_from_phase=None,
        )
        self.runtime = SimpleNamespace(active_task=lambda _cycle: self.active)

    def snapshot(self):
        return self.before

    def resume_snapshot(self, _kind):
        return self.before


@pytest.mark.parametrize("kind", ["PLAN_APPROVAL", "RESULT_APPROVAL"])
def test_durable_command_resume_is_unchanged_and_traced(kind):
    sink = CaptureSink()
    tracer = AgentTracer(sink)
    callback = AgentTraceCallbackHandler(tracer, context_provider=_context)

    authority = FakeAuthority()

    class Agent:
        state = None
        config = None

        def invoke(self, state, *, config, durability, context):
            self.state = state
            self.config = config
            assert durability == "sync"
            assert context == "repo-context"
            authority.active = SimpleNamespace(
                task_run_id="task-run-A",
                phase=TaskPhase.EXECUTING,
                waiting_from_phase=None,
            )
            return {"ok": True}

    agent = Agent()
    occurrence = f"{kind.lower()}-occurrence"
    invoke_workflow_phase(
        agent,
        authority=authority,
        thread_id="github:1:issue:12",
        prompt="",
        context="repo-context",
        resume={"kind": kind, "occurrence_key": occurrence},
        trace_callback=callback,
        tracer=tracer,
        trace_context=_context(),
    )

    assert isinstance(agent.state, Command)
    assert agent.config["configurable"] == {"thread_id": "github:1:issue:12"}
    assert agent.config["callbacks"] == [callback]
    assert any(
        item.category == "RESUME" and f"{kind} {occurrence}" == item.message
        for item in sink.events
    )


@pytest.mark.parametrize("kind", ["PLAN_APPROVAL", "RESULT_APPROVAL", "CLARIFICATION"])
def test_interrupt_identity_is_traced_without_checkpoint_payload(kind):
    sink = CaptureSink()
    tracer = AgentTracer(sink)
    occurrence = f"{kind.lower()}-occurrence"

    class Agent:
        def invoke(self, *_args, **_kwargs):
            return {
                "__interrupt__": (
                    SimpleNamespace(
                        value={
                            "kind": kind,
                            "occurrence_key": occurrence,
                            "large": "x" * 20_000,
                        }
                    ),
                )
            }

    invoke_workflow_phase(
        Agent(),
        authority=FakeAuthority(),
        thread_id="github:1:issue:12",
        prompt="plan",
        tracer=tracer,
        trace_context=_context(),
    )

    interrupt_event = next(item for item in sink.events if item.category == "INTERRUPT")
    assert interrupt_event.message == f"{kind} {occurrence}"
    assert "large" not in interrupt_event.message
