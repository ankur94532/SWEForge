"""Encrypted, repository-scoped runtime credentials for registered tools."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .github_store import SQLiteGitHubStore

MIN_SECRET_CHARS = 8
_SECRET_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


class SecretValue:
    """A short-lived plaintext wrapper whose string representations are safe."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        self.__value = value

    def reveal(self) -> str:
        """Reveal only at the registered process/transport injection boundary."""
        return self.__value

    def __repr__(self) -> str:
        return "SecretValue('[REDACTED]')"

    def __str__(self) -> str:
        return "[REDACTED]"


def load_secret_master_key() -> bytes | None:
    """Load an operator-owned Fernet key without inventing local key storage."""
    value = os.getenv("SWEFORGE_SECRET_MASTER_KEY")
    if value:
        return value.strip().encode()
    key_path = os.getenv("SWEFORGE_SECRET_MASTER_KEY_FILE")
    if not key_path:
        return None
    path = Path(key_path).expanduser().resolve(strict=True)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("secret master-key file permissions must be 0600 or stricter")
    return path.read_bytes().strip()


class RepoSecretStore:
    """Encrypted-at-rest provider; repo identity is always application supplied."""

    def __init__(self, store: SQLiteGitHubStore, master_key: bytes | str) -> None:
        self.store = store
        key = master_key.encode() if isinstance(master_key, str) else master_key
        try:
            self._cipher = Fernet(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("repository secret master key is invalid") from exc

    @classmethod
    def from_environment(
        cls, store: SQLiteGitHubStore, *, required: bool = False
    ) -> RepoSecretStore | None:
        key = load_secret_master_key()
        if key is None:
            if required:
                raise ValueError(
                    "SWEFORGE_SECRET_MASTER_KEY or "
                    "SWEFORGE_SECRET_MASTER_KEY_FILE is required"
                )
            return None
        return cls(store, key)

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str) or not _SECRET_NAME.fullmatch(name):
            raise ValueError("repository secret name is malformed")
        return name

    def set(
        self,
        repo_id: int,
        name: str,
        value: str,
        *,
        now: str | None = None,
    ) -> None:
        key = self._validate_name(name)
        if not isinstance(value, str) or len(value) < MIN_SECRET_CHARS:
            raise ValueError(
                "repository secret values must contain at least "
                f"{MIN_SECRET_CHARS} characters"
            )
        timestamp = now or datetime.now(UTC).isoformat()
        ciphertext = self._cipher.encrypt(value.encode())
        with self.store.transaction(immediate=True) as db:
            if (
                db.execute(
                    "SELECT 1 FROM repositories WHERE repo_id=?", (repo_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("repository has not been observed")
            db.execute(
                """INSERT INTO repo_secrets_v1(
                   repo_id,name,ciphertext,created_at,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(repo_id,name) DO UPDATE SET
                   ciphertext=excluded.ciphertext,updated_at=excluded.updated_at""",
                (repo_id, key, ciphertext, timestamp, timestamp),
            )
            self._audit(db, repo_id, key, "SET", None, 1, timestamp)

    def get(self, repo_id: int, name: str) -> SecretValue | None:
        key = self._validate_name(name)
        row = self.store.connection.execute(
            "SELECT ciphertext FROM repo_secrets_v1 WHERE repo_id=? AND name=?",
            (repo_id, key),
        ).fetchone()
        if row is None:
            return None
        try:
            value = self._cipher.decrypt(bytes(row["ciphertext"])).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise RuntimeError("repository secret cannot be decrypted") from exc
        return SecretValue(value)

    def delete(self, repo_id: int, name: str, *, now: str | None = None) -> bool:
        key = self._validate_name(name)
        timestamp = now or datetime.now(UTC).isoformat()
        with self.store.transaction(immediate=True) as db:
            cursor = db.execute(
                "DELETE FROM repo_secrets_v1 WHERE repo_id=? AND name=?",
                (repo_id, key),
            )
            if cursor.rowcount:
                self._audit(db, repo_id, key, "DELETE", None, 1, timestamp)
            return bool(cursor.rowcount)

    def list_names(self, repo_id: int) -> tuple[str, ...]:
        rows = self.store.connection.execute(
            "SELECT name FROM repo_secrets_v1 WHERE repo_id=? ORDER BY name",
            (repo_id,),
        ).fetchall()
        return tuple(str(row["name"]) for row in rows)

    def exists(self, repo_id: int, name: str) -> bool:
        return self.get(repo_id, name) is not None

    def resolve_env(
        self,
        repo_id: int,
        references: Mapping[str, str],
        *,
        subject: str,
        now: str | None = None,
    ) -> dict[str, SecretValue]:
        """Resolve only the exact trusted reference mapping for one invocation."""
        resolved: dict[str, SecretValue] = {}
        missing: list[str] = []
        for environment_name, secret_name in references.items():
            value = self.get(repo_id, secret_name)
            if value is None:
                missing.append(secret_name)
            else:
                resolved[environment_name] = value
        timestamp = now or datetime.now(UTC).isoformat()
        with self.store.transaction(immediate=True) as db:
            self._audit(
                db,
                repo_id,
                None,
                "RESOLVE" if not missing else "RESOLVE_MISSING",
                subject,
                len(resolved),
                timestamp,
            )
        if missing:
            names = ", ".join(sorted(missing))
            raise PermissionError(
                f"Required repository credential is not configured: {names}. "
                f"Use `sweforge secret set OWNER/REPO {missing[0]}`."
            )
        return resolved

    @staticmethod
    def _audit(db, repo_id, name, operation, subject, count, created_at) -> None:
        db.execute(
            """INSERT INTO repo_secret_audit_v1(
               repo_id,name,operation,subject,count,created_at) VALUES(?,?,?,?,?,?)""",
            (repo_id, name, operation, subject, count, created_at),
        )
