"""Transport failures must not discard an entire review.

A K7 batch lost 170 complete reviews to 153 OpenAIConnectionError blips
because every model was built with max_retries=0: one dropped connection on
a late call threw away all preceding work in that review.
"""

from sweforge import reviewer
from sweforge.config import MODEL_TRANSIENT_RETRIES


class _Recorder:
    def __init__(self):
        self.kwargs = None

    def __call__(self, model, **kwargs):
        self.kwargs = kwargs
        return _Fake()


class _Fake:
    def with_structured_output(self, *a, **k):
        return self

    def bind_tools(self, *a, **k):
        return self


def test_the_retry_budget_is_bounded_and_positive():
    """Zero would restore the original defect; unbounded would mask an
    unhealthy provider."""
    assert 0 < MODEL_TRANSIENT_RETRIES <= 5


def test_the_reviewer_model_retries_transport_failures(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(reviewer, "init_chat_model", recorder)
    reviewer.bounded_model("openai:gpt-5.6-luna")
    assert recorder.kwargs["max_retries"] == MODEL_TRANSIENT_RETRIES


def test_the_reviewer_keeps_its_hard_request_timeout(monkeypatch):
    """Retries must not become a way to wait forever."""
    recorder = _Recorder()
    monkeypatch.setattr(reviewer, "init_chat_model", recorder)
    reviewer.bounded_model("openai:gpt-5.6-luna")
    assert recorder.kwargs["timeout"] == reviewer.MODEL_REQUEST_TIMEOUT_SECONDS


def test_every_init_chat_model_call_passes_the_retry_budget():
    """Structural check over the whole package.

    Two of the four sites invoke inline rather than through a builder, and a
    site added later would reintroduce the defect silently. Parsing the AST
    catches every call regardless of how it is named or reached.
    """
    import ast
    import pathlib

    offenders = []
    for path in pathlib.Path("src/sweforge").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "init_chat_model":
                continue
            kw = {k.arg: k.value for k in node.keywords}
            value = kw.get("max_retries")
            ok = isinstance(value, ast.Name) and value.id == "MODEL_TRANSIENT_RETRIES"
            if not ok:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"model sites without a retry budget: {offenders}"


def test_the_ast_check_would_catch_a_regression(tmp_path):
    """Positive control: the check must fail on a site that opts out, or it
    would pass even if every call were reverted to max_retries=0."""
    import ast

    tree = ast.parse("init_chat_model(model, max_retries=0, timeout=60)")
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    kw = {k.arg: k.value for k in call.keywords}
    value = kw.get("max_retries")
    assert not (isinstance(value, ast.Name) and value.id == "MODEL_TRANSIENT_RETRIES")


def test_no_model_site_is_left_at_zero_retries():
    """Textual backstop for the same property."""
    import pathlib

    src = pathlib.Path("src/sweforge")
    offenders = [f.name for f in src.glob("*.py") if "max_retries=0" in f.read_text()]
    assert not offenders, f"these still discard reviews on a blip: {offenders}"
