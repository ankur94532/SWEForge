"""Safe errors shared by GitHub authentication and REST clients."""


class GitHubAPIError(RuntimeError):
    """A safe GitHub API error that never includes response bodies or headers."""
