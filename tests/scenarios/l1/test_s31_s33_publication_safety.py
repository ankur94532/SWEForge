"""S31-S33: publication correctness.

Publication is the only step that writes to the world, so its failures are the
ones that cannot be taken back. S31 covers the partial-publication window: a
crash between commit and push must resume without a second commit or a second
pull request. S32 covers a diverged remote, which must be refused rather than
force-pushed. S33 covers an ambiguous match, which must fail closed rather than
guess which pull request it meant.
"""

import subprocess

import pytest
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from test_github_publisher import Client, TokenProvider, git, setup_publication

from sweforge.github_publisher import GitHubPublisher
from sweforge.workspace import WorkspaceError


def _prepare(root_dir):
    """setup_publication builds its tree inside an existing directory."""
    root_dir.mkdir(parents=True, exist_ok=True)
    return setup_publication(root_dir)


def _publisher(store, client, root, remote):
    return GitHubPublisher(
        store=store,
        client=client,
        token_provider=TokenProvider(),
        lock_root=root / "locks",
        remote_url_factory=lambda _: f"file://{remote}",
    )


def _observation(store) -> Observation:
    """Publication scenarios assert against the store they drove."""
    return Observation(events=[], store=store)


@scenario(
    "S31",
    layer=Layer.L1,
    invariants=["INV-PUB-MAPPING", "INV-NO-FALSE-RESOLUTION"],
    description="A resumed publication makes no second commit and no second PR.",
)
def s31_resume_after_partial_publication(root_dir) -> Observation:
    store, event_key, remote = _prepare(root_dir)
    client = Client()
    publisher = _publisher(store, client, root_dir, remote)

    first = publisher.publish_one()
    assert first.status == "COMPLETED", first.error
    publication = store.publication_for_event(event_key)
    commit = publication.local_commit_sha
    assert commit and publication.pr_number == 41

    # The restart: publishing again must find no work rather than redo it.
    again = publisher.publish_one()
    assert again.status == "NO_WORK", f"a resumed publication redid work: {again}"
    assert len(client.pull_requests_created) == 1, (
        f"resume created {len(client.pull_requests_created)} pull requests"
    )
    after = store.publication_for_event(event_key)
    assert after.local_commit_sha == commit, "resume moved the recorded commit"
    assert after.pr_number == 41, "resume rebound the pull request"
    return _observation(store)


@scenario(
    "S32",
    layer=Layer.L1,
    invariants=["INV-PUB-MAPPING"],
    description="A diverged remote branch is refused, never force-pushed.",
)
def s32_divergent_remote_is_refused(root_dir) -> Observation:
    store, event_key, remote = _prepare(root_dir)
    client = Client()
    publisher = _publisher(store, client, root_dir, remote)

    first = publisher.publish_one()
    assert first.status == "COMPLETED", first.error
    publication = store.publication_for_event(event_key)
    branch = publication.branch_name
    pushed = git(remote, "rev-parse", f"refs/heads/{branch}")

    # Someone else moves the remote branch to an unrelated commit.
    scratch = root_dir / "other"
    subprocess.run(
        ["git", "clone", "--quiet", str(remote), str(scratch)],
        check=True,
        capture_output=True,
    )
    git(scratch, "checkout", "-q", "-B", branch)
    (scratch / "SOMEONE_ELSE.md").write_text("divergent\n")
    git(scratch, "add", "-A")
    git(
        scratch,
        "-c",
        "user.email=o@e.com",
        "-c",
        "user.name=O",
        "commit",
        "-qm",
        "theirs",
    )
    git(scratch, "push", "-q", "--force", "origin", branch)
    diverged = git(remote, "rev-parse", f"refs/heads/{branch}")
    assert diverged != pushed, "the remote was not actually diverged"

    # A further publication attempt must refuse rather than overwrite it.
    store.connection.execute(
        "UPDATE logical_publications SET status='PENDING',remote_commit_sha=NULL "
        "WHERE source_event_key=?",
        (event_key,),
    )
    store.connection.commit()
    result = publisher.publish_one()
    assert result.status != "COMPLETED", "a diverged remote was published over"
    assert git(remote, "rev-parse", f"refs/heads/{branch}") == diverged, (
        "the diverged remote commit was overwritten"
    )
    return _observation(store)


@scenario(
    "S33",
    layer=Layer.L1,
    invariants=["INV-PUB-MAPPING"],
    description="Two matching pull requests fail closed rather than pick one.",
)
def s33_ambiguous_pull_request(root_dir) -> Observation:
    store, event_key, remote = _prepare(root_dir)
    client = Client()
    # Two pull requests already match this head and base.
    client.pull_requests_created = [
        {"number": 41, "html_url": "https://example/41"},
        {"number": 42, "html_url": "https://example/42"},
    ]
    publisher = _publisher(store, client, root_dir, remote)

    with pytest.raises(WorkspaceError, match="ambiguous"):
        _raise_from(publisher)

    publication = store.publication_for_event(event_key)
    assert publication is None or publication.pr_number is None, (
        "an ambiguous match was bound to a pull request anyway"
    )
    return _observation(store)


def _raise_from(publisher):
    """publish_one records the error; re-raise it so the refusal is asserted."""
    result = publisher.publish_one()
    if result.error:
        raise WorkspaceError(result.error)
    raise AssertionError(f"ambiguity was not refused: {result.status}")


@pytest.mark.parametrize("scenario_id", ["S31", "S32", "S33"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower(), layer=Layer.L1)
    assert result.ok, "\n" + result.report()
