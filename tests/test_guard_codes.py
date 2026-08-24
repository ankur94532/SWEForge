import json
from pathlib import Path

from sweforge.guard_codes import GuardCode, GuardProblem


def test_every_guard_code_is_classified_exactly_once():
    path = Path(__file__).parents[1] / "acceptance" / "guard_classification.json"
    classification = json.loads(path.read_text())
    assert set(classification) == {code.value for code in GuardCode}
    assert all(
        item["class"] in {"A", "B", "UNKNOWN"} for item in classification.values()
    )
    assert all(item.get("rationale") for item in classification.values())
    assert any(item["class"] != "B" for item in classification.values())


def test_guard_problem_uses_dataclass_equality_and_hashing():
    first = GuardProblem(GuardCode.RC_REQUIREMENT_COVERAGE, "x")
    second = GuardProblem(GuardCode.RC_REQUIREMENT_COVERAGE, "x")
    assert first == second
    assert len({first, "x"}) == 2
