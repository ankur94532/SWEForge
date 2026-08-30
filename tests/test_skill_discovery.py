from types import SimpleNamespace

import pytest

from sweforge.agent_trace import AgentTracer
from sweforge.repo_memory import SQLiteMemoryStore, repo_skills_namespace
from sweforge.skills import (
    DEFAULT_WORKFLOW_SKILLS,
    MAX_SKILL_DESCRIPTION_CHARS,
    canonical_skill_path,
    ensure_default_repo_skills,
    parse_skill_metadata,
    put_repo_skill,
    show_repo_skill,
)
from sweforge.workflow_driver import DeepAgentWorkflowDriver
from sweforge.workflow_runtime import WorkflowCycleKind
from sweforge.workflow_spec import DEFAULT_WORKFLOW


class TraceSink:
    def emit(self, _event):
        pass


def test_frontmatter_metadata_is_normalized_and_has_a_canonical_path():
    metadata = parse_skill_metadata(
        "reporting",
        """---
name: reporting
description: >-
  Prepare compatible reports while preserving
  declaration order.
---

# Reporting body
""",
    )

    assert metadata.name == "reporting"
    assert metadata.description == (
        "Prepare compatible reports while preserving declaration order."
    )
    assert metadata.path == "/skills/reporting/SKILL.md"


def test_legacy_skill_without_frontmatter_has_a_deterministic_fallback():
    first = parse_skill_metadata("legacy", "# Existing instructions\nDo work.")
    second = parse_skill_metadata("legacy", "Different legacy body.")

    assert first == second
    assert first.description == "Trusted skill legacy; load for full instructions."


@pytest.mark.parametrize(
    "content",
    [
        "---\nname: reporting\ndescription: missing delimiter",
        "---not-frontmatter\nname: reporting\ndescription: invalid\n---",
        "---\nname: reporting\n---\nbody",
        "---\nname: reporting\nname: reporting\ndescription: duplicate\n---",
        "---\nname: other\ndescription: mismatch\n---",
        "---\nname: ' reporting '\ndescription: inexact name\n---",
        "---\n? [not, scalar]\n: value\nname: reporting\ndescription: bad key\n---",
    ],
)
def test_malformed_duplicate_or_mismatched_metadata_fails_closed(content):
    with pytest.raises(ValueError):
        parse_skill_metadata("reporting", content)


def test_overlong_description_fails_closed():
    content = (
        "---\nname: reporting\ndescription: "
        + "x" * (MAX_SKILL_DESCRIPTION_CHARS + 1)
        + "\n---\nbody"
    )

    with pytest.raises(ValueError, match="too long"):
        parse_skill_metadata("reporting", content)


@pytest.mark.parametrize("name", ["../other", "two/parts", "", "-leading"])
def test_canonical_skill_path_rejects_invalid_identity(name):
    with pytest.raises(ValueError, match="malformed"):
        canonical_skill_path(name)


def test_bundled_default_skills_have_valid_bounded_metadata():
    for name, content in DEFAULT_WORKFLOW_SKILLS.items():
        metadata = parse_skill_metadata(name, content)
        assert metadata.name == name
        assert len(metadata.description) <= MAX_SKILL_DESCRIPTION_CHARS
        assert metadata.path == f"/skills/{name}/SKILL.md"


def test_default_skill_seeding_adds_only_missing_requested_paths():
    memory = SQLiteMemoryStore(":memory:")
    operator_content = "# Operator planning instructions"
    put_repo_skill(
        memory.store,
        101,
        "implementation-planning/SKILL.md",
        operator_content,
    )
    malformed_operator_value = {"content": 42, "encoding": "utf-8"}
    memory.store.put(
        repo_skills_namespace(101),
        "/implementation-validation/SKILL.md",
        malformed_operator_value,
    )

    first = ensure_default_repo_skills(
        memory.store,
        101,
        (
            "implementation-planning",
            "implementation-execution",
            "implementation-validation",
            "unrelated-custom-skill",
        ),
    )
    second = ensure_default_repo_skills(
        memory.store,
        101,
        ("implementation-planning", "implementation-execution"),
    )

    assert first == 1
    assert second == 0
    assert (
        show_repo_skill(memory.store, 101, "implementation-planning/SKILL.md")
        == operator_content
    )
    assert (
        show_repo_skill(memory.store, 101, "implementation-execution/SKILL.md")
        == DEFAULT_WORKFLOW_SKILLS["implementation-execution"]
    )
    assert (
        show_repo_skill(memory.store, 101, "implementation-validation/SKILL.md") is None
    )
    stored = memory.store.get(
        repo_skills_namespace(101), "/implementation-validation/SKILL.md"
    )
    assert stored is not None
    assert stored.value == malformed_operator_value
    assert (
        show_repo_skill(memory.store, 202, "implementation-execution/SKILL.md") is None
    )


def test_driver_reconstruction_seeds_default_paths_and_propagates_tracer(
    monkeypatch, tmp_path
):
    memory = SQLiteMemoryStore(":memory:")
    tracer = AgentTracer(TraceSink())
    captured = {}

    def fake_builder(**kwargs):
        captured.update(kwargs)
        return "agent"

    monkeypatch.setattr(
        "sweforge.workflow_driver.build_durable_workflow_agent", fake_builder
    )
    driver = DeepAgentWorkflowDriver(
        runtime=SimpleNamespace(),
        workflow_cycle_id="workflow-cycle",
        spec=DEFAULT_WORKFLOW,
        store=SimpleNamespace(),
        client=SimpleNamespace(),
        worktree=tmp_path,
        planning_model="planning",
        execution_model="execution",
        validation_model="validation",
        checkpointer=object(),
        memory_store=memory.store,
        capability_registry=None,
        sandbox_backend_provider=None,
        secure_execution=False,
        unsafe_local_shell=True,
        tracer=tracer,
    )
    driver._root_event = lambda _cycle: {
        "repo_id": 101,
        "repo_full_name": "owner/repo",
    }
    cycle = SimpleNamespace(
        workflow_cycle_id="workflow-cycle",
        thread_id="thread",
        cycle_kind=WorkflowCycleKind.INITIAL,
    )

    agent, _authority, context = driver._agent(cycle)

    assert agent == "agent"
    assert context.repo_id == 101
    assert captured["tracer"] is tracer
    for name, content in DEFAULT_WORKFLOW_SKILLS.items():
        assert show_repo_skill(memory.store, 101, f"{name}/SKILL.md") == content
