"""Deterministic doubles for the model-backed roles.

SWEForge injects planner, reviewer, curator and clarification roles as plain
callables, so an offline scenario substitutes a scripted callable rather than a
chat model. Chat-level doubles are provided too, for tests that exercise the
agent harness itself.

RealModel is deliberately absent: a real role is just the provider model string
handed to the engine, so wrapping it would add a layer that could drift from
what production does.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from sweforge.reviewer import ExecutionReviewResult


@dataclass
class ScriptedPlanner:
    """Returns canned plan text and records every invocation."""

    plans: list[str] = field(default_factory=lambda: ["1. edit README\n2. run tests"])
    calls: list[dict] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.plans) - 1)
        return self.plans[index]


@dataclass
class ScriptedReviewer:
    """Returns canned verdicts in order, then repeats the last one.

    Repeating rather than raising is deliberate: a repair loop may review more
    times than a scenario scripted, and the scenario should fail on its own
    invariants rather than on the double running out.
    """

    verdicts: list[str] = field(default_factory=lambda: ["ACCEPT"])
    summary: str = "scripted review"
    calls: list[dict] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> ExecutionReviewResult:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.verdicts) - 1)
        return ExecutionReviewResult(verdict=self.verdicts[index], summary=self.summary)


class RoleExhausted(RuntimeError):
    """A replay double was asked for more responses than were recorded."""


@dataclass
class ReplayRole:
    """Replays recorded responses in order, refusing to invent more.

    Running past the cassette raises rather than repeating: a replayed run that
    silently improvises is no longer a replay, and the resulting PASS would be
    about the double rather than the code.
    """

    responses: Sequence[Any]
    calls: list[dict] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if len(self.calls) > len(self.responses):
            raise RoleExhausted(
                f"replay cassette has {len(self.responses)} response(s) but was "
                f"called {len(self.calls)} time(s)"
            )
        return self.responses[len(self.calls) - 1]


@dataclass
class FaultyRole:
    """Wraps any role callable and fails at chosen call indices.

    Indices are 1-based and explicit; nothing is probabilistic, so a scenario
    that expects the second call to fail asserts exactly that.
    """

    inner: Callable[..., Any]
    fail_on_calls: Iterable[int] = ()
    error: BaseException | None = None
    calls: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._failing = set(self.fail_on_calls)

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if len(self.calls) in self._failing:
            raise self.error or RuntimeError(
                f"injected role failure on call {len(self.calls)}"
            )
        return self.inner(**kwargs)


class ScriptedChatModel(BaseChatModel):
    """Deterministic chat model that records the tool surfaces it was bound to."""

    _responses: list[AIMessage] = PrivateAttr()
    _surfaces: list[tuple[str, ...]] = PrivateAttr(default_factory=list)
    _invocations: list[list] = PrivateAttr(default_factory=list)

    def __init__(self, responses: list[AIMessage]):
        super().__init__()
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "sweforge-harness-scripted"

    @property
    def surfaces(self) -> list[tuple[str, ...]]:
        return self._surfaces

    @property
    def invocations(self) -> list[list]:
        return self._invocations

    def bind_tools(self, tools, **kwargs):
        names = []
        for item in tools:
            if isinstance(item, dict):
                function = item.get("function", {})
                names.append(item.get("name") or function.get("name") or "")
            else:
                names.append(getattr(item, "name", ""))
        self._surfaces.append(tuple(names))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self._invocations.append(list(messages))
        if not self._responses:
            raise RoleExhausted("scripted chat model ran out of responses")
        message = self._responses.pop(0)
        return ChatResult(generations=[ChatGeneration(message=message)])
