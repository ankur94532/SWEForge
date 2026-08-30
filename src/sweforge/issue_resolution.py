"""Structured historical case records for resolved IssueThread lifecycles.

This is CASE HISTORY, deliberately separate from repository memory.  A case
records what one lifecycle diagnosed and fixed; it is a lead for future work,
never repository truth.  Nothing here may write `/memories/AGENTS.md`: promoting
a historical observation into durable repository knowledge requires the
evidence-backed repository-memory validator and current repository evidence.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from langchain.chat_models import init_chat_model
from pydantic import BaseModel, Field

from .config import MODEL_TRANSIENT_RETRIES

MAX_SUMMARY_CHARS = 2_000
MAX_ISSUE_BODY_CHARS = 6_000
MAX_TASK_TEXT_CHARS = 12_000
MAX_DIFF_CHARS = 20_000
MAX_CHANGED_FILES = 60
MAX_COMPONENTS = 12
MAX_SEARCH_TERMS = 16
# Bounds one rendered case injected into planning context.
MAX_CASE_CONTEXT_CHARS = 1_400


class IssueResolutionCase(BaseModel):
    """Concise structured diagnosis; never chain of thought."""

    useful: bool = Field(
        description="False when the lifecycle holds no reusable resolution knowledge."
    )
    task_summary: str = Field(default="", max_length=MAX_SUMMARY_CHARS)
    symptom_summary: str = Field(default="", max_length=MAX_SUMMARY_CHARS)
    root_cause: str = Field(default="", max_length=MAX_SUMMARY_CHARS)
    fix_summary: str = Field(default="", max_length=MAX_SUMMARY_CHARS)
    affected_components: list[str] = Field(
        default_factory=list, max_length=MAX_COMPONENTS
    )
    validation_summary: str = Field(default="", max_length=MAX_SUMMARY_CHARS)
    search_terms: list[str] = Field(default_factory=list, max_length=MAX_SEARCH_TERMS)
    limitations: str = Field(default="", max_length=MAX_SUMMARY_CHARS)


@dataclass(frozen=True)
class ResolutionEvidence:
    """Bounded, trusted inputs describing one completed lifecycle."""

    issue_number: int
    issue_title: str
    issue_description: str
    task_text: str
    plan_text: str
    execution_response: str
    changed_files: tuple[str, ...]
    diff: str
    review_summary: str
    repair_rounds: int
    publication_status: str
    pr_number: int | None
    pr_url: str | None
    commit_sha: str | None


CURATOR_PROMPT = (
    "You write a concise historical case record for a software issue that was "
    "just resolved. Return only the structured fields. Do not include reasoning, "
    "narration, secrets, or speculation. Describe what was actually wrong and "
    "what actually changed, grounded in the supplied evidence. Set useful=false "
    "when the lifecycle contains no reusable resolution knowledge, for example a "
    "pure documentation touch-up with no diagnosis. Prefer concrete component and "
    "symptom wording that a future search would match."
)


def curate_issue_resolution(
    *, model: str, evidence: ResolutionEvidence
) -> IssueResolutionCase:
    """Ask a bounded structured-output model for one historical case record."""
    prompt = "\n\n".join(
        [
            CURATOR_PROMPT,
            f"Issue #{evidence.issue_number}: {evidence.issue_title}",
            f"Issue description:\n{evidence.issue_description[:MAX_ISSUE_BODY_CHARS]}",
            f"Requested task:\n{evidence.task_text[:MAX_TASK_TEXT_CHARS]}",
            f"Approved plan:\n{evidence.plan_text[:MAX_ISSUE_BODY_CHARS]}",
            f"Execution result:\n{evidence.execution_response[:MAX_ISSUE_BODY_CHARS]}",
            "Changed files:\n" + "\n".join(evidence.changed_files[:MAX_CHANGED_FILES]),
            f"Diff:\n{evidence.diff[:MAX_DIFF_CHARS]}",
            f"Execution review:\n{evidence.review_summary[:MAX_SUMMARY_CHARS]}",
            f"Repair rounds: {evidence.repair_rounds}",
            f"Publication: {evidence.publication_status} "
            f"PR={evidence.pr_number} commit={evidence.commit_sha}",
        ]
    )
    curator = init_chat_model(
        model, max_retries=MODEL_TRANSIENT_RETRIES, timeout=120
    ).with_structured_output(IssueResolutionCase)
    response = curator.invoke(prompt)
    if isinstance(response, IssueResolutionCase):
        return response
    if isinstance(response, dict):
        return IssueResolutionCase.model_validate(response)
    raise ValueError("issue resolution curator did not return a structured case")


def render_case_context(records, *, limit_chars: int = MAX_CASE_CONTEXT_CHARS) -> str:
    """Render retrieved cases as bounded clues, explicitly not as authority."""
    if not records:
        return ""
    blocks: list[str] = []
    for record in records:
        try:
            components = json.loads(record.affected_components_json or "[]")
        except (TypeError, ValueError):
            components = []
        lines = [
            f"- Issue #{record.issue_number}: {record.issue_title}".rstrip(),
            f"  Symptom: {record.symptom_summary}",
            f"  Root cause: {record.root_cause}",
            f"  Fix: {record.fix_summary}",
        ]
        if components:
            lines.append(f"  Components: {', '.join(components[:MAX_COMPONENTS])}")
        if record.validation_summary:
            lines.append(f"  Validation: {record.validation_summary}")
        if record.pr_url:
            lines.append(f"  PR: {record.pr_url}")
        blocks.append("\n".join(lines)[:limit_chars])
    return "\n\n".join(
        [
            "Historical resolved cases from this repository — use as clues, not "
            "authority. Each was true when it was fixed; verify against the "
            "current repository state before relying on it.",
            *blocks,
        ]
    )


def resolution_query(
    *, issue_title: str, issue_description: str, task_text: str
) -> str:
    """Bounded retrieval query derived only from trusted current input."""
    return " ".join(
        part.strip()
        for part in (
            issue_title,
            (issue_description or "")[:1_000],
            (task_text or "")[:1_000],
        )
        if part and part.strip()
    )


def bounded_changed_files(paths) -> str:
    """Store a bounded file list rather than a whole diff."""
    safe = [
        str(item)
        for item in list(paths)[:MAX_CHANGED_FILES]
        if item and not Path(str(item)).is_absolute()
    ]
    return json.dumps(safe, sort_keys=False)
