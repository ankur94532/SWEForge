"""Every reviewer model path must be bounded.

An unbounded provider request blocks a worker forever: a stalled connection
held a conformance batch for 62 minutes on 3 seconds of CPU, producing neither
a result nor an error. Retry policy belongs to SWEForge's dispatcher backoff,
not to the provider SDK, so SDK retries stay at zero.
"""

import inspect

from sweforge import reviewer


def test_bounded_model_sets_timeout_and_disables_sdk_retries(monkeypatch):
    captured = {}

    def fake_init(model, **kwargs):
        captured.update({"model": model, **kwargs})
        return object()

    monkeypatch.setattr(reviewer, "init_chat_model", fake_init)
    reviewer.bounded_model("provider:some-model")
    assert captured["model"] == "provider:some-model"
    assert captured["timeout"] == reviewer.MODEL_REQUEST_TIMEOUT_SECONDS
    assert captured["max_retries"] == 0


def test_no_create_agent_call_receives_a_bare_model_string():
    """A bare string makes create_agent build an unbounded client of its own.

    Passing the string between internal helpers is fine — they bound it before
    constructing anything. Only the construction sites matter.
    """
    source = inspect.getsource(reviewer)
    offenders = []
    for chunk in source.split("create_agent(")[1:]:
        head = chunk.split(")", 1)[0]
        if "model=model," in head:
            offenders.append(head.strip().splitlines()[0])
    assert not offenders, (
        f"{len(offenders)} create_agent call(s) build an unbounded client; "
        "route them through bounded_model()"
    )


def test_every_agent_builder_uses_the_bounded_constructor():
    for name in ("build_reviewer", "_build_challenger", "_build_finalizer"):
        source = inspect.getsource(getattr(reviewer, name))
        assert "bounded_model(model)" in source, f"{name} is unbounded"
