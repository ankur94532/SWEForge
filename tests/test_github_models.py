from sweforge.github_models import (
    SubjectKind,
    classify_subject,
    contains_agent_mention,
)


def test_agent_mention_matching():
    assert contains_agent_mention("@agent")
    assert contains_agent_mention("Please @AGENT fix this")
    assert not contains_agent_mention("@agentic is a different account")
    assert not contains_agent_mention(None)


def test_pull_request_classification_uses_marker():
    assert classify_subject({}) == SubjectKind.ISSUE
    assert classify_subject({"pull_request": {"url": "https://example/pr/1"}}) == (
        SubjectKind.PULL_REQUEST
    )
