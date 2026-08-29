from pathlib import Path

import pytest

from sweforge.workflow_spec import load_workflow_spec, parse_workflow_spec


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
