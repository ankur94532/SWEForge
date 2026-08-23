"""Evidence-backed, application-controlled repository memory learning."""

import fcntl
import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from langchain.chat_models import init_chat_model
from langgraph.store.base import BaseStore
from pydantic import BaseModel, Field

from .repo_memory import append_repo_memory, read_repo_memory, repo_memory_namespace

SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*\S+|"
    r"-----BEGIN [A-Z ]+ PRIVATE KEY-----|sk-[A-Za-z0-9]{20,}"
)
MAX_CANDIDATES = 20
MAX_FACT_CHARS = 1_000
MAX_EXCERPT_CHARS = 2_000
MAX_CATALOG_LINES_PER_FILE = 80
MAX_CATALOG_FILES = 100


class MemoryLearningStatus(StrEnum):
    PENDING = "PENDING"
    UPDATED = "UPDATED"
    NO_UPDATE = "NO_UPDATE"
    FAILED = "FAILED"


class RepoMemoryEvidence(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    excerpt: str = Field(min_length=1, max_length=MAX_EXCERPT_CHARS)


class RepoMemoryProposal(BaseModel):
    category: str = Field(min_length=1, max_length=40)
    fact: str = Field(min_length=1, max_length=MAX_FACT_CHARS)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    durability_reason: str = Field(min_length=1, max_length=500)


class RepoMemoryCandidate(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=40)
    fact: str = Field(min_length=1, max_length=MAX_FACT_CHARS)
    evidence: list[RepoMemoryEvidence] = Field(min_length=1, max_length=10)
    durability_reason: str = Field(min_length=1, max_length=500)


class RepoMemoryCuratorResponse(BaseModel):
    proposals: list[RepoMemoryProposal] = Field(default_factory=list, max_length=20)


@dataclass(frozen=True)
class RepoMemoryEvidenceItem:
    evidence_id: str
    path: str
    start_line: int
    end_line: int
    content_hash: str
    excerpt: str


@dataclass(frozen=True)
class CuratorOutput:
    candidates: list[RepoMemoryCandidate]
    proposal_json: str


@dataclass(frozen=True)
class MemoryLearningResult:
    status: MemoryLearningStatus
    accepted_candidates: int = 0
    rejected_candidates: int = 0
    error: str | None = None


def curate_repository_memory(
    *,
    model: str,
    repo_id: int,
    worktree: str | Path,
    changed_files: list[str],
    diff: str,
    existing_memory: str,
    plan_text: str,
) -> CuratorOutput:
    """Ask a bounded read-only model for structured, evidence-backed candidates."""
    root = Path(worktree).resolve()
    catalog: list[RepoMemoryEvidenceItem] = []
    for relative in sorted(set(changed_files))[:MAX_CATALOG_FILES]:
        path = _safe_path(root, relative)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for start in range(0, min(len(lines), MAX_CATALOG_LINES_PER_FILE), 20):
            selected = lines[start : start + 20]
            if not selected:
                continue
            excerpt = "\n".join(selected)
            identity = f"{relative}:{start + 1}:{start + len(selected)}:{excerpt}"
            catalog.append(
                RepoMemoryEvidenceItem(
                    evidence_id=hashlib.sha256(identity.encode()).hexdigest()[:20],
                    path=relative,
                    start_line=start + 1,
                    end_line=start + len(selected),
                    content_hash=hashlib.sha256(excerpt.encode()).hexdigest(),
                    excerpt=excerpt[:MAX_EXCERPT_CHARS],
                )
            )
    catalog_text = "\n".join(
        f"[{item.evidence_id}] {item.path}:{item.start_line}-{item.end_line}\n"
        + "\n".join(
            f"{number}: {line}"
            for number, line in zip(
                range(item.start_line, item.end_line + 1), item.excerpt.splitlines()
            )
        )
        for item in catalog
    )
    prompt = (
        "You are a repository-memory curator. Return only structured proposals. "
        "Propose durable knowledge useful to future tasks in this same repository. "
        "Return zero candidates when evidence is temporary, generic, uncertain, "
        "issue-specific, secret-like, or duplicated. Every candidate must cite an "
        "one or more evidence_ids from the supplied catalog. Never invent IDs, paths, "
        "line ranges, excerpts, or hashes; the application resolves evidence IDs. "
        "Never use "
        "issue text or model assertions as sole evidence. Do not write files.\n\n"
        f"Existing repository memory:\n{existing_memory[:12_000]}\n\n"
        f"Accepted plan (supplemental only):\n{plan_text[:12_000]}\n\n"
        f"Changed files:\n{', '.join(changed_files[:100])}\n\n"
        f"Cumulative diff:\n{diff[:40_000]}\n\nEvidence catalog (line-numbered):\n"
        + catalog_text[:60_000]
    )
    curator = init_chat_model(model, max_retries=0, timeout=120).with_structured_output(
        RepoMemoryCuratorResponse
    )
    response = curator.invoke(prompt)
    if isinstance(response, RepoMemoryCuratorResponse):
        parsed = response
    elif isinstance(response, dict):
        parsed = RepoMemoryCuratorResponse.model_validate(response)
    else:
        raise ValueError("memory curator did not return structured proposals")
    by_id = {item.evidence_id: item for item in catalog}
    candidates: list[RepoMemoryCandidate] = []
    for proposal in parsed.proposals:
        evidence_items = [by_id.get(item_id) for item_id in proposal.evidence_ids]
        if any(item is None for item in evidence_items):
            raise ValueError("memory curator referenced an unknown evidence ID")
        evidence = [
            RepoMemoryEvidence(
                path=item.path,
                start_line=item.start_line,
                end_line=item.end_line,
                content_hash=item.content_hash,
                excerpt=item.excerpt,
            )
            for item in evidence_items
            if item is not None
        ]
        candidates.append(
            RepoMemoryCandidate(
                candidate_id=_memory_entry_id(
                    repo_id=repo_id,
                    category=proposal.category,
                    fact=proposal.fact,
                    content_hashes=[
                        item.content_hash for item in evidence_items if item is not None
                    ],
                ),
                category=proposal.category,
                fact=proposal.fact,
                evidence=evidence,
                durability_reason=proposal.durability_reason,
            )
        )
    return CuratorOutput(
        candidates=candidates,
        proposal_json=json.dumps(
            [candidate.model_dump() for candidate in candidates], sort_keys=True
        ),
    )


def _memory_entry_id(
    *, repo_id: int, category: str, fact: str, content_hashes: list[str]
) -> str:
    """Content identity of one durable memory entry, used for deduplication."""
    material = "\0".join(
        [str(repo_id), category.casefold(), fact.casefold(), *sorted(content_hashes)]
    )
    return hashlib.sha256(material.encode()).hexdigest()[:24]


def candidate_from_proposal(
    *,
    repo_id: int,
    worktree: str | Path,
    category: str,
    fact: str,
    durability_reason: str,
    path: str,
    start_line: int,
    end_line: int,
) -> RepoMemoryCandidate:
    """Build a candidate from a nominated location, deriving the evidence here.

    The proposer only says WHERE to look.  The excerpt and hash are read from
    the authoritative worktree so a model can never assert repository content
    it did not actually find.
    """
    root = Path(worktree).resolve()
    target = _safe_path(root, path)
    lines = target.read_text(encoding="utf-8").splitlines()
    if start_line < 1 or end_line < start_line:
        raise ValueError("memory evidence range is inverted")
    if end_line > len(lines):
        raise ValueError("memory evidence range is outside the file")
    excerpt = "\n".join(lines[start_line - 1 : end_line])
    if not excerpt.strip():
        raise ValueError("memory evidence range is empty")
    if SECRET_RE.search(excerpt):
        raise ValueError("memory evidence appears to contain a secret")
    content_hash = hashlib.sha256(excerpt.encode()).hexdigest()
    return RepoMemoryCandidate(
        candidate_id=_memory_entry_id(
            repo_id=repo_id,
            category=category,
            fact=fact,
            content_hashes=[content_hash],
        ),
        category=category,
        fact=fact,
        evidence=[
            RepoMemoryEvidence(
                path=path,
                start_line=start_line,
                end_line=end_line,
                content_hash=content_hash,
                excerpt=excerpt[:MAX_EXCERPT_CHARS],
            )
        ],
        durability_reason=durability_reason,
    )


@contextmanager
def repo_memory_lock(lock_root: str | Path, repo_id: int) -> Iterator[None]:
    root = Path(lock_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"repo-memory-{repo_id}.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_path(worktree: Path, path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("memory evidence path escapes the repository")
    resolved = (worktree / candidate).resolve()
    resolved.relative_to(worktree.resolve())
    return resolved


def validate_candidate(
    candidate: RepoMemoryCandidate, *, repo_id: int, worktree: str | Path
) -> None:
    del repo_id  # Repository identity is supplied by the caller's trusted context.
    root = Path(worktree).resolve()
    if SECRET_RE.search(candidate.fact) or SECRET_RE.search(
        candidate.durability_reason
    ):
        raise ValueError("memory candidate appears to contain a secret")
    if not candidate.fact.strip() or candidate.fact.strip().startswith(("I ", "We ")):
        raise ValueError("memory candidate is task-specific or empty")
    for evidence in candidate.evidence:
        path = _safe_path(root, evidence.path)
        if evidence.end_line < evidence.start_line:
            raise ValueError("memory evidence range is inverted")
        lines = path.read_text(encoding="utf-8").splitlines()
        if evidence.end_line > len(lines):
            raise ValueError("memory evidence range is outside the file")
        actual = "\n".join(lines[evidence.start_line - 1 : evidence.end_line])
        if hashlib.sha256(actual.encode()).hexdigest() != evidence.content_hash:
            raise ValueError("memory evidence content hash does not match")
        if actual[:MAX_EXCERPT_CHARS] != evidence.excerpt[:MAX_EXCERPT_CHARS]:
            raise ValueError("memory evidence excerpt does not match")
        if SECRET_RE.search(actual) or SECRET_RE.search(evidence.excerpt):
            raise ValueError("memory evidence appears to contain a secret")


def apply_memory_candidates(
    store: BaseStore,
    *,
    repo_id: int,
    worktree: str | Path,
    candidates: list[RepoMemoryCandidate],
    lock_root: str | Path,
) -> MemoryLearningResult:
    """Validate and append novel candidates under a repo-only mutation lock."""
    if len(candidates) > MAX_CANDIDATES:
        return MemoryLearningResult(
            MemoryLearningStatus.FAILED, error="too many candidates"
        )
    accepted: list[RepoMemoryCandidate] = []
    rejected = 0
    for candidate in candidates:
        try:
            validate_candidate(candidate, repo_id=repo_id, worktree=worktree)
        except (OSError, UnicodeError, ValueError):
            rejected += 1
            continue
        accepted.append(candidate)
    if not accepted:
        return MemoryLearningResult(
            MemoryLearningStatus.NO_UPDATE,
            rejected_candidates=rejected,
        )
    namespace = repo_memory_namespace(repo_id)
    with repo_memory_lock(lock_root, repo_id):
        current = read_repo_memory(store, namespace) or "# SWEForge Repository Memory\n"
        existing = {line.strip().casefold() for line in current.splitlines()}
        existing_ids = set(re.findall(r"sweforge-memory-entry:([0-9a-f]+)", current))
        novel = [
            candidate
            for candidate in accepted
            if candidate.candidate_id not in existing_ids
            and not any(candidate.fact.strip().casefold() in line for line in existing)
        ]
        if not novel:
            return MemoryLearningResult(
                MemoryLearningStatus.NO_UPDATE,
                rejected_candidates=rejected,
            )
        sections = [
            f"- [{candidate.category}] {candidate.fact.strip()}\n"
            f"<!-- sweforge-memory-entry:{candidate.candidate_id} -->"
            for candidate in novel
        ]
        append_repo_memory(store, namespace, "\n".join(sections))
    return MemoryLearningResult(
        MemoryLearningStatus.UPDATED,
        accepted_candidates=len(novel),
        rejected_candidates=rejected,
    )
