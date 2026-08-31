from pathlib import Path

import pytest

from sweforge.workflow_spec import (
    derive_revision_spec,
    load_workflow_spec,
    parse_workflow_spec,
)


def document(order=("A", "B", "C", "D")):
    dependencies = {"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C"]}
    return {
        "version": 1,
        "workflow_id": "diamond",
        "tasks": [
            {
                "id": task_id,
                "depends_on": dependencies[task_id],
                "planning": {
                    "skill": f"{task_id}-planning",
                    "tools": ["read_file", "glob", "grep"],
                },
                "execution": {
                    "skill": f"{task_id}-execution",
                    "tools": ["read_file", "write_file", "edit_file", "execute"],
                },
                "validation": {
                    "skill": f"{task_id}-validation",
                    "tools": ["read_file", "glob", "grep", "run_validation"],
                },
            }
            for task_id in order
        ],
    }


def test_spec_preserves_declaration_order_and_has_stable_digest():
    first = parse_workflow_spec(document())
    second = parse_workflow_spec(document())
    assert [task.id for task in first.tasks] == ["A", "B", "C", "D"]
    assert first.digest == second.digest


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["tasks"].append(value["tasks"][0]), "duplicate task"),
        (lambda value: value["tasks"][1].update(depends_on=["missing"]), "unknown"),
        (lambda value: value["tasks"][0].update(depends_on=["D"]), "cycle"),
        (
            lambda value: value["tasks"][0]["execution"].update(tools=["root_shell"]),
            "unknown tools",
        ),
        (lambda value: value["tasks"][0]["planning"].pop("skill"), "required"),
        (lambda value: value.update(version=2), "version"),
    ],
)
def test_invalid_specs_fail_before_execution(mutate, message):
    value = document()
    mutate(value)
    with pytest.raises(ValueError, match=message):
        parse_workflow_spec(value)


def test_skill_resolver_rejects_missing_operator_skill():
    with pytest.raises(ValueError, match="does not exist"):
        parse_workflow_spec(document(), skill_exists=lambda name: name != "A-planning")


def test_loader_uses_only_explicit_operator_path(tmp_path: Path):
    path = tmp_path / "trusted.yaml"
    path.write_text(
        """version: 1
workflow_id: simple
tasks:
  - id: implementation
    depends_on: []
    planning: {skill: plan, tools: [read_file]}
    execution: {skill: execute, tools: [read_file, edit_file]}
    validation: {skill: validate, tools: [read_file, run_validation]}
"""
    )
    spec = load_workflow_spec(path)
    assert spec.workflow_id == "simple"
    with pytest.raises(FileNotFoundError):
        load_workflow_spec(tmp_path / "repository-controlled.yaml")


def phase_skill_document():
    """Two tasks whose phases carry visibly distinct, overlapping skill sets."""
    return {
        "version": 1,
        "workflow_id": "phase-skills",
        "tasks": [
            {
                "id": "api",
                "depends_on": [],
                "planning": {
                    "skill": "api-planning",
                    "skills": ["architecture-research", "shared"],
                    "tools": ["read_file", "glob"],
                },
                "execution": {
                    "skill": "java-backend",
                    "skills": ["shared"],
                    "tools": ["read_file", "edit_file", "execute"],
                },
                "validation": {
                    "skill": "api-validation",
                    "tools": ["read_file", "run_validation"],
                },
            },
            {
                "id": "db",
                "depends_on": ["api"],
                "planning": {
                    "skill": "db-planning",
                    "skills": ["architecture-research"],
                    "tools": ["read_file", "grep"],
                },
                "execution": {
                    "skill": "postgres",
                    "skills": ["migration-execution", "java-backend"],
                    "tools": ["read_file", "write_file", "execute"],
                },
                "validation": {
                    "skill": "db-validation",
                    "skills": ["api-validation"],
                    "tools": ["read_file", "run_validation"],
                },
            },
        ],
    }


def test_revision_spec_is_one_owner_with_phase_wise_skill_and_tool_unions():
    initial = parse_workflow_spec(document())
    revision = derive_revision_spec(initial)

    assert revision.workflow_id == f"revision-{initial.digest[:16]}"
    assert [task.id for task in revision.tasks] == ["revision"]
    task = revision.tasks[0]
    for select, phase in (
        (lambda item: item.planning, task.planning),
        (lambda item: item.execution, task.execution),
        (lambda item: item.validation, task.validation),
    ):
        assert phase.skills == tuple(
            skill for original in initial.tasks for skill in select(original).skills
        )
    assert {"write_file", "edit_file", "execute"}.isdisjoint(task.planning.tools)
    assert {"write_file", "edit_file", "execute"} <= set(task.execution.tools)
    assert "run_validation" in task.validation.tools
    assert revision == derive_revision_spec(initial)


def test_revision_skills_are_derived_independently_per_phase():
    revision = derive_revision_spec(parse_workflow_spec(phase_skill_document()))
    task = revision.tasks[0]

    assert task.planning.skills == (
        "api-planning",
        "architecture-research",
        "shared",
        "db-planning",
    )
    assert task.execution.skills == (
        "java-backend",
        "shared",
        "postgres",
        "migration-execution",
    )
    assert task.validation.skills == ("api-validation", "db-validation")


@pytest.mark.parametrize(
    ("skill", "present_in"),
    [
        ("api-planning", "planning"),
        ("architecture-research", "planning"),
        ("db-planning", "planning"),
        ("postgres", "execution"),
        ("migration-execution", "execution"),
        ("api-validation", "validation"),
        ("db-validation", "validation"),
    ],
)
def test_phase_only_revision_skills_never_leak_into_another_phase(skill, present_in):
    task = derive_revision_spec(parse_workflow_spec(phase_skill_document())).tasks[0]
    phases = {
        "planning": task.planning,
        "execution": task.execution,
        "validation": task.validation,
    }
    assert skill in phases.pop(present_in).skills
    for name, phase in phases.items():
        assert skill not in phase.skills, f"{skill} leaked into revision {name}"


def test_a_skill_trusted_in_two_phases_appears_in_both_derived_sets():
    task = derive_revision_spec(parse_workflow_spec(phase_skill_document())).tasks[0]
    # "shared" is trusted in planning and execution, never in validation.
    assert "shared" in task.planning.skills
    assert "shared" in task.execution.skills
    assert "shared" not in task.validation.skills


def test_repeated_revision_skills_are_deduplicated_within_their_phase():
    task = derive_revision_spec(parse_workflow_spec(phase_skill_document())).tasks[0]
    for phase in (task.planning, task.execution, task.validation):
        assert len(phase.skills) == len(set(phase.skills))
    # "java-backend" is named by both tasks' execution phases, "shared" by both
    # api planning and api execution, "api-validation" by both validations.
    assert task.execution.skills.count("java-backend") == 1
    assert task.validation.skills.count("api-validation") == 1


def test_revision_primary_and_additional_skills_split_per_phase():
    task = derive_revision_spec(parse_workflow_spec(phase_skill_document())).tasks[0]
    assert task.planning.skill == "api-planning"
    assert task.planning.additional_skills == (
        "architecture-research",
        "shared",
        "db-planning",
    )
    assert task.execution.skill == "java-backend"
    assert task.execution.additional_skills == (
        "shared",
        "postgres",
        "migration-execution",
    )
    assert task.validation.skill == "api-validation"
    assert task.validation.additional_skills == ("db-validation",)


def test_revision_tool_unions_are_unchanged_by_phase_wise_skills():
    initial = parse_workflow_spec(phase_skill_document())
    task = derive_revision_spec(initial).tasks[0]
    research = ("read_file", "glob", "grep")

    # Planning keeps its own union minus mutation, plus trusted research.
    assert set(task.planning.tools) == {"read_file", "glob", "grep"}
    assert {"write_file", "edit_file", "execute"}.isdisjoint(task.planning.tools)
    # Execution and validation keep their own union plus trusted research.
    assert set(task.execution.tools) == {
        "read_file",
        "edit_file",
        "execute",
        "write_file",
        *research,
    }
    assert set(task.validation.tools) == {"read_file", "run_validation", *research}


def test_revision_derivation_is_deterministic_across_calls():
    initial = parse_workflow_spec(phase_skill_document())
    assert derive_revision_spec(initial) == derive_revision_spec(initial)
    assert derive_revision_spec(initial).digest == derive_revision_spec(initial).digest
