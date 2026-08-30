"""Trusted, versioned declarative workflow specifications.

Workflow specifications are authorization input.  Callers must load them from
an operator-selected path; this module deliberately has no repository discovery
fallback.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

SCHEMA_VERSION = 1
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

# Deep Agents built-ins plus SWEForge lifecycle/research tools.  Operator-owned
# MCP names can be supplied to ``load_workflow_spec`` through ``known_tools``.
BUILTIN_WORKFLOW_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "glob",
        "grep",
        "write_file",
        "edit_file",
        "execute",
        "run_validation",
        "request_clarification",
        "search_issue_memory",
        "propose_repo_memory",
    }
)


@dataclass(frozen=True, slots=True)
class PhaseSpec:
    skill: str
    tools: tuple[str, ...]
    additional_skills: tuple[str, ...] = ()

    @property
    def skills(self) -> tuple[str, ...]:
        return (self.skill, *self.additional_skills)


@dataclass(frozen=True, slots=True)
class TaskSpec:
    id: str
    depends_on: tuple[str, ...]
    planning: PhaseSpec
    execution: PhaseSpec
    validation: PhaseSpec


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    version: int
    workflow_id: str
    tasks: tuple[TaskSpec, ...]
    digest: str

    @property
    def task_map(self) -> Mapping[str, TaskSpec]:
        return MappingProxyType({task.id: task for task in self.tasks})

    def canonical_document(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "workflow_id": self.workflow_id,
            "tasks": [
                {
                    "id": task.id,
                    "depends_on": list(task.depends_on),
                    "planning": {
                        "skill": task.planning.skill,
                        "tools": list(task.planning.tools),
                        **(
                            {"skills": list(task.planning.additional_skills)}
                            if task.planning.additional_skills
                            else {}
                        ),
                    },
                    "execution": {
                        "skill": task.execution.skill,
                        "tools": list(task.execution.tools),
                        **(
                            {"skills": list(task.execution.additional_skills)}
                            if task.execution.additional_skills
                            else {}
                        ),
                    },
                    "validation": {
                        "skill": task.validation.skill,
                        "tools": list(task.validation.tools),
                        **(
                            {"skills": list(task.validation.additional_skills)}
                            if task.validation.additional_skills
                            else {}
                        ),
                    },
                }
                for task in self.tasks
            ],
        }


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return value


def _phase(
    value: object,
    *,
    label: str,
    known_tools: frozenset[str],
    skill_exists: Any,
) -> PhaseSpec:
    item = _mapping(value, label)
    unknown_fields = set(item) - {"skill", "skills", "tools"}
    if unknown_fields:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown_fields)}")
    skill = item.get("skill")
    if not isinstance(skill, str) or not skill.strip():
        raise ValueError(f"{label}.skill is required")
    skill = skill.strip()
    if not _ID.fullmatch(skill):
        raise ValueError(f"{label}.skill is malformed")
    if skill_exists is not None and not skill_exists(skill):
        raise ValueError(f"{label}.skill does not exist: {skill}")
    raw_additional_skills = item.get("skills", [])
    if not isinstance(raw_additional_skills, list) or any(
        not isinstance(name, str) or not _ID.fullmatch(name)
        for name in raw_additional_skills
    ):
        raise ValueError(f"{label}.skills must be a list of skill names")
    if skill in raw_additional_skills or len(raw_additional_skills) != len(
        set(raw_additional_skills)
    ):
        raise ValueError(f"{label}.skills contains duplicates")
    if skill_exists is not None:
        missing_skills = [
            name for name in raw_additional_skills if not skill_exists(name)
        ]
        if missing_skills:
            raise ValueError(f"{label}.skills do not exist: {missing_skills}")
    raw_tools = item.get("tools")
    if not isinstance(raw_tools, list) or any(
        not isinstance(name, str) or not name for name in raw_tools
    ):
        raise ValueError(f"{label}.tools must be a list of names")
    if len(raw_tools) != len(set(raw_tools)):
        raise ValueError(f"{label}.tools contains duplicates")
    unknown_tools = sorted(set(raw_tools) - known_tools)
    if unknown_tools:
        raise ValueError(f"{label}.tools contains unknown tools: {unknown_tools}")
    return PhaseSpec(
        skill=skill,
        tools=tuple(raw_tools),
        additional_skills=tuple(raw_additional_skills),
    )


def parse_workflow_spec(
    document: object,
    *,
    known_tools: set[str] | frozenset[str] = BUILTIN_WORKFLOW_TOOLS,
    skill_exists: Any = None,
) -> WorkflowSpec:
    """Validate a decoded operator document and return its immutable form."""
    root = _mapping(document, "workflow")
    unknown_fields = set(root) - {"version", "workflow_id", "tasks"}
    if unknown_fields:
        raise ValueError(f"workflow has unknown fields: {sorted(unknown_fields)}")
    if root.get("version") != SCHEMA_VERSION:
        raise ValueError(f"workflow version must be {SCHEMA_VERSION}")
    workflow_id = root.get("workflow_id")
    if not isinstance(workflow_id, str) or not _ID.fullmatch(workflow_id):
        raise ValueError("workflow_id is malformed")
    raw_tasks = root.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("workflow.tasks must be a non-empty list")
    allowed_tools = frozenset(known_tools)
    tasks: list[TaskSpec] = []
    seen: set[str] = set()
    for index, raw_task in enumerate(raw_tasks):
        label = f"tasks[{index}]"
        item = _mapping(raw_task, label)
        unknown_task_fields = set(item) - {
            "id",
            "depends_on",
            "planning",
            "execution",
            "validation",
        }
        if unknown_task_fields:
            raise ValueError(
                f"{label} has unknown fields: {sorted(unknown_task_fields)}"
            )
        task_id = item.get("id")
        if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
            raise ValueError(f"{label}.id is malformed")
        if task_id in seen:
            raise ValueError(f"duplicate task ID: {task_id}")
        seen.add(task_id)
        dependencies = item.get("depends_on")
        if not isinstance(dependencies, list) or any(
            not isinstance(dep, str) or not _ID.fullmatch(dep) for dep in dependencies
        ):
            raise ValueError(f"{label}.depends_on must be a list of task IDs")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"{label}.depends_on contains duplicates")
        tasks.append(
            TaskSpec(
                id=task_id,
                depends_on=tuple(dependencies),
                planning=_phase(
                    item.get("planning"),
                    label=f"{label}.planning",
                    known_tools=allowed_tools,
                    skill_exists=skill_exists,
                ),
                execution=_phase(
                    item.get("execution"),
                    label=f"{label}.execution",
                    known_tools=allowed_tools,
                    skill_exists=skill_exists,
                ),
                validation=_phase(
                    item.get("validation"),
                    label=f"{label}.validation",
                    known_tools=allowed_tools,
                    skill_exists=skill_exists,
                ),
            )
        )
    missing = sorted(
        {dep for task in tasks for dep in task.depends_on if dep not in seen}
    )
    if missing:
        raise ValueError(f"workflow references unknown dependencies: {missing}")
    graph = {task.id: task.depends_on for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("workflow dependency cycle detected")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task in tasks:
        visit(task.id)
    canonical = {
        "version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "tasks": [
            {
                "id": task.id,
                "depends_on": list(task.depends_on),
                "planning": {
                    "skill": task.planning.skill,
                    "tools": list(task.planning.tools),
                    **(
                        {"skills": list(task.planning.additional_skills)}
                        if task.planning.additional_skills
                        else {}
                    ),
                },
                "execution": {
                    "skill": task.execution.skill,
                    "tools": list(task.execution.tools),
                    **(
                        {"skills": list(task.execution.additional_skills)}
                        if task.execution.additional_skills
                        else {}
                    ),
                },
                "validation": {
                    "skill": task.validation.skill,
                    "tools": list(task.validation.tools),
                    **(
                        {"skills": list(task.validation.additional_skills)}
                        if task.validation.additional_skills
                        else {}
                    ),
                },
            }
            for task in tasks
        ],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return WorkflowSpec(SCHEMA_VERSION, workflow_id, tuple(tasks), digest)


def derive_revision_spec(initial: WorkflowSpec) -> WorkflowSpec:
    """Derive the exact trusted one-owner revision envelope from ``initial``."""

    def ordered(values):
        return list(dict.fromkeys(values))

    all_skills = ordered(
        skill
        for task in initial.tasks
        for phase in (task.planning, task.execution, task.validation)
        for skill in phase.skills
    )
    research = {"ls", "read_file", "glob", "grep", "search_issue_memory"}
    trusted_research = ordered(
        tool
        for task in initial.tasks
        for phase in (task.planning, task.execution, task.validation)
        for tool in phase.tools
        if tool in research
    )
    planning_tools = ordered(
        [
            tool
            for task in initial.tasks
            for tool in task.planning.tools
            if tool not in {"write_file", "edit_file", "execute"}
        ]
        + trusted_research
    )
    execution_tools = ordered(
        [tool for task in initial.tasks for tool in task.execution.tools]
        + trusted_research
    )
    validation_tools = ordered(
        [tool for task in initial.tasks for tool in task.validation.tools]
        + trusted_research
    )
    workflow_id = f"revision-{initial.digest[:16]}"
    document = {
        "version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "tasks": [
            {
                "id": "revision",
                "depends_on": [],
                "planning": {
                    "skill": all_skills[0],
                    "skills": all_skills[1:],
                    "tools": planning_tools,
                },
                "execution": {
                    "skill": all_skills[0],
                    "skills": all_skills[1:],
                    "tools": execution_tools,
                },
                "validation": {
                    "skill": all_skills[0],
                    "skills": all_skills[1:],
                    "tools": validation_tools,
                },
            }
        ],
    }
    known_tools = {
        tool
        for task in initial.tasks
        for phase in (task.planning, task.execution, task.validation)
        for tool in phase.tools
    }
    return parse_workflow_spec(document, known_tools=known_tools)


def load_workflow_spec(
    path: str | Path,
    *,
    known_tools: set[str] | frozenset[str] = BUILTIN_WORKFLOW_TOOLS,
    skill_exists: Any = None,
) -> WorkflowSpec:
    """Load only the explicit operator path; never consult the target repo."""
    operator_path = Path(path).expanduser().resolve(strict=True)
    if not operator_path.is_file():
        raise ValueError("workflow specification is not a regular file")
    try:
        document = yaml.safe_load(operator_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid workflow specification: {exc}") from exc
    return parse_workflow_spec(
        document, known_tools=known_tools, skill_exists=skill_exists
    )


DEFAULT_WORKFLOW = parse_workflow_spec(
    {
        "version": 1,
        "workflow_id": "default",
        "tasks": [
            {
                "id": "implementation",
                "depends_on": [],
                "planning": {
                    "skill": "implementation-planning",
                    "tools": [
                        "ls",
                        "read_file",
                        "glob",
                        "grep",
                        "search_issue_memory",
                    ],
                },
                "execution": {
                    "skill": "implementation-execution",
                    "tools": [
                        "ls",
                        "read_file",
                        "glob",
                        "grep",
                        "write_file",
                        "edit_file",
                        "execute",
                        "request_clarification",
                        "propose_repo_memory",
                    ],
                },
                "validation": {
                    "skill": "implementation-validation",
                    "tools": [
                        "ls",
                        "read_file",
                        "glob",
                        "grep",
                        "run_validation",
                    ],
                },
            }
        ],
    }
)
