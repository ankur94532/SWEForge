import json
from pathlib import Path

from sweforge.guard_codes import GuardCode


def test_every_guard_code_is_classified_exactly_once():
    path = Path(__file__).parents[1] / "acceptance" / "guard_classification.json"
    classification = json.loads(path.read_text())
    assert set(classification) == {code.value for code in GuardCode}
    assert all(
        item["class"] in {"A", "B", "UNKNOWN"} for item in classification.values()
    )
    assert all(item.get("rationale") for item in classification.values())
