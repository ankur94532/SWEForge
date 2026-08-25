"""Mechanical PRIMARY protection for live acceptance runs.

The campaign's rule is that the stress scenarios never touch PRIMARY. Rules
enforced by care get broken; this one is enforced by a function that every
mutating live action must call first.

Fails closed by construction: a repository is refused unless it was explicitly
allowlisted, so forgetting to configure the allowlist blocks the run rather
than silently permitting whatever was passed.
"""

import os
from dataclasses import dataclass

ALLOWLIST_ENV = "SWEFORGE_ACCEPTANCE_REPOS"
PRIMARY_ENV = "SWEFORGE_PRIMARY_REPOS"

# Repositories that must never be targeted by a stress scenario, whatever the
# allowlist says. Kept in code as well as the environment so an empty or
# mistyped environment variable cannot quietly disarm the protection.
PRIMARY_REPOS: frozenset[str] = frozenset()


class PrimaryRepositoryRefused(RuntimeError):
    """A live action targeted a PRIMARY repository."""


class RepositoryNotAllowlisted(RuntimeError):
    """A live action targeted a repository that was never allowlisted."""


@dataclass(frozen=True, slots=True)
class Allowlist:
    allowed: frozenset[str]
    primary: frozenset[str]

    @classmethod
    def from_env(cls) -> "Allowlist":
        def parse(name: str) -> frozenset[str]:
            raw = os.environ.get(name, "")
            return frozenset(
                item.strip() for item in raw.replace(",", " ").split() if item.strip()
            )

        return cls(
            allowed=parse(ALLOWLIST_ENV), primary=parse(PRIMARY_ENV) | PRIMARY_REPOS
        )

    def check(self, repo_full_name: str) -> None:
        """Raise unless this repository is a safe live target.

        PRIMARY is checked first and independently: a repository named in both
        lists is refused, so an allowlist entry can never override protection.
        """
        name = (repo_full_name or "").strip()
        if not name:
            raise RepositoryNotAllowlisted(
                "no repository was named; a live action must state its target"
            )
        if name in self.primary:
            raise PrimaryRepositoryRefused(
                f"{name} is a PRIMARY repository and is refused during the "
                "stress campaign; the PRIMARY acceptance set runs separately "
                "after the exit condition holds"
            )
        if name not in self.allowed:
            raise RepositoryNotAllowlisted(
                f"{name} is not in the acceptance allowlist "
                f"({ALLOWLIST_ENV}); refusing to act on it. Allowlisted: "
                f"{sorted(self.allowed) or 'none'}"
            )


# Every decision this guard makes, in order. E8's evidence is what the
# allowlist actually decided, not a scenario's claim about what it targeted.
_AUDIT: list[dict] = []


def drain_audit() -> list[dict]:
    """Take the recorded decisions and reset, so runs never share entries."""
    entries = list(_AUDIT)
    _AUDIT.clear()
    return entries


def check_live_target(repo_full_name: str) -> None:
    """Guard every mutating live action. Call before touching GitHub."""
    name = (repo_full_name or "").strip()
    allowlist = Allowlist.from_env()
    entry = {
        "repository": name,
        "target_is_primary": name in allowlist.primary,
        "allowed": False,
    }
    try:
        allowlist.check(repo_full_name)
    except Exception:
        _AUDIT.append(entry)
        raise
    entry["allowed"] = True
    _AUDIT.append(entry)
