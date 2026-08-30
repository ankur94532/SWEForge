"""Trusted immutable repository configuration bundles and generations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import yaml

from .capabilities import (
    REMOTE_MCP_TRANSPORTS,
    MCPServerSpec,
    RepoCapabilityRegistry,
)
from .github_store import SQLiteGitHubStore
from .skills import SkillMetadata, parse_skill_metadata
from .workflow_spec import BUILTIN_WORKFLOW_TOOLS, WorkflowSpec, parse_workflow_spec

MAX_BUNDLE_FILE_BYTES = 1_000_000
MAX_BUNDLE_BYTES = 10_000_000
MAX_SCRIPT_TIMEOUT_SECONDS = 600
MAX_SCRIPT_OUTPUT_CHARS = 20_000
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_RUNTIMES = frozenset({"python", "shell"})
_EFFECTS = frozenset({"read", "mutate"})


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous duplicate keys in every trusted bundle YAML file."""


def _construct_unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing repository configuration",
                node.start_mark,
                "configuration keys must be scalar values",
                key_node.start_mark,
            ) from exc
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing repository configuration",
                node.start_mark,
                f"duplicate configuration key: {key}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


@dataclass(frozen=True, slots=True)
class ScriptToolSpec:
    name: str
    description: str
    runtime: str
    entrypoint: str
    args_schema: Mapping[str, Any]
    timeout_seconds: int
    effect: str
    directory: str
    env: Mapping[str, str] = MappingProxyType({})
    secret_env: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class MCPBundleServer:
    server_id: str
    connection: Mapping[str, Any]
    tools: tuple[str, ...]
    secret_env: Mapping[str, str] = MappingProxyType({})
    secret_headers: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ValidatedRepoBundle:
    root: Path
    digest: str
    workflow: WorkflowSpec
    workflow_document: Mapping[str, Any]
    skills: tuple[SkillMetadata, ...]
    scripts: tuple[ScriptToolSpec, ...]
    mcp_servers: tuple[MCPBundleServer, ...]
    files: Mapping[str, str]
    manifest: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RepoConfigGeneration:
    generation_id: str
    repo_id: int
    generation: int
    digest: str
    workflow_id: str
    workflow_version: int
    created_at: str
    manifest: Mapping[str, Any]


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _load_yaml(content: str, label: str) -> dict[str, Any]:
    try:
        value = yaml.load(content, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    return _mapping(value, label)


def _validate_args_schema(value: object, label: str) -> dict[str, Any]:
    schema = _mapping(value, label)
    allowed = {"type", "properties", "required", "additionalProperties"}
    unknown = set(schema) - allowed
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")
    if schema.get("type") != "object":
        raise ValueError(f"{label}.type must be object")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or any(
        not isinstance(name, str) or not isinstance(item, dict)
        for name, item in properties.items()
    ):
        raise ValueError(f"{label}.properties must be a mapping")
    allowed_types = {"string", "integer", "number", "boolean", "array", "object"}
    for name, item in properties.items():
        if item.get("type") not in allowed_types:
            raise ValueError(f"{label}.properties.{name}.type is invalid")
    required = schema.get("required", [])
    if (
        not isinstance(required, list)
        or any(not isinstance(name, str) for name in required)
        or len(required) != len(set(required))
        or not set(required) <= set(properties)
    ):
        raise ValueError(f"{label}.required is invalid")
    if schema.get("additionalProperties", False) is not False:
        raise ValueError(f"{label}.additionalProperties must be false")
    return schema


def _string_mapping(value: object, label: str, *, env_names: bool) -> dict[str, str]:
    if value is None:
        return {}
    result = _mapping(value, label)
    if any(
        not isinstance(key, str)
        or not isinstance(item, str)
        or not item
        or (env_names and not _ENV_NAME.fullmatch(key))
        for key, item in result.items()
    ):
        raise ValueError(f"{label} must map valid names to non-empty strings")
    return {str(key): str(item) for key, item in result.items()}


def _header_mapping(value: object, label: str) -> dict[str, str]:
    result = _string_mapping(value, label, env_names=False)
    normalized: set[str] = set()
    for name, item in result.items():
        folded = name.casefold()
        if (
            not _HTTP_HEADER_NAME.fullmatch(name)
            or "\r" in item
            or "\n" in item
            or folded in normalized
        ):
            raise ValueError(f"{label} contains an invalid or duplicate HTTP header")
        normalized.add(folded)
    return result


def _header_collision(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    return bool(
        {name.casefold() for name in left} & {name.casefold() for name in right}
    )


def _read_bundle_files(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise ValueError("repository configuration bundle is not a directory")
    files: dict[str, str] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"bundle symlinks are forbidden: {path.relative_to(root)}")
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("bundle file escapes its trusted root") from exc
        data = path.read_bytes()
        if not data or len(data) > MAX_BUNDLE_FILE_BYTES:
            raise ValueError(
                f"bundle file is empty or too large: {path.relative_to(root)}"
            )
        total += len(data)
        if total > MAX_BUNDLE_BYTES:
            raise ValueError("repository configuration bundle is too large")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"bundle file is not UTF-8: {path.relative_to(root)}"
            ) from exc
        relative = path.relative_to(root).as_posix()
        if not (
            relative == "workflow.yaml"
            or relative.startswith("skills/")
            or relative.startswith("tools/scripts/")
            or relative.startswith("tools/mcp/")
        ):
            raise ValueError(f"unexpected bundle path: {relative}")
        files[relative] = content
    return files


def _parse_skills(root: Path, files: Mapping[str, str]) -> tuple[SkillMetadata, ...]:
    skill_root = root / "skills"
    if not skill_root.is_dir():
        raise ValueError("bundle skills directory is required")
    metadata: list[SkillMetadata] = []
    for directory in sorted(path for path in skill_root.iterdir() if path.is_dir()):
        if not _NAME.fullmatch(directory.name):
            raise ValueError(f"skill directory name is malformed: {directory.name}")
        relative = f"skills/{directory.name}/SKILL.md"
        content = files.get(relative)
        if content is None:
            raise ValueError(f"skill is missing SKILL.md: {directory.name}")
        metadata.append(parse_skill_metadata(directory.name, content))
    if not metadata:
        raise ValueError("bundle must contain at least one skill")
    return tuple(metadata)


def _parse_scripts(root: Path, files: Mapping[str, str]) -> tuple[ScriptToolSpec, ...]:
    scripts_root = root / "tools" / "scripts"
    if not scripts_root.exists():
        return ()
    if not scripts_root.is_dir():
        raise ValueError("tools/scripts must be a directory")
    result: list[ScriptToolSpec] = []
    names: set[str] = set()
    for directory in sorted(path for path in scripts_root.iterdir() if path.is_dir()):
        label = f"script tool {directory.name}"
        tool_path = f"tools/scripts/{directory.name}/tool.yaml"
        content = files.get(tool_path)
        if content is None:
            raise ValueError(f"{label} is missing tool.yaml")
        item = _load_yaml(content, label)
        allowed = {
            "version",
            "name",
            "description",
            "runtime",
            "entrypoint",
            "args_schema",
            "timeout_seconds",
            "effect",
            "env",
            "secret_env",
        }
        unknown = set(item) - allowed
        if unknown:
            raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")
        name = item.get("name")
        description = item.get("description")
        runtime = item.get("runtime")
        entrypoint = item.get("entrypoint")
        effect = item.get("effect")
        timeout = item.get("timeout_seconds", 30)
        if item.get("version") != 1:
            raise ValueError(f"{label}.version must be 1")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
            raise ValueError(f"{label}.name is malformed or duplicated")
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > 300
        ):
            raise ValueError(f"{label}.description is required and bounded")
        if runtime not in _RUNTIMES:
            raise ValueError(f"{label}.runtime must be python or shell")
        if not isinstance(entrypoint, str) or not entrypoint:
            raise ValueError(f"{label}.entrypoint is required")
        entry = PurePosixPath(entrypoint)
        if entry.is_absolute() or ".." in entry.parts or "." in entry.parts:
            raise ValueError(f"{label}.entrypoint traversal is forbidden")
        entry_key = f"tools/scripts/{directory.name}/{entry.as_posix()}"
        if entry_key not in files:
            raise ValueError(f"{label}.entrypoint does not exist")
        entry_path = (directory / entrypoint).resolve()
        try:
            entry_path.relative_to(directory.resolve())
        except ValueError as exc:
            raise ValueError(f"{label}.entrypoint escapes its tool directory") from exc
        if effect not in _EFFECTS:
            raise ValueError(f"{label}.effect must be read or mutate")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or not 1 <= timeout <= MAX_SCRIPT_TIMEOUT_SECONDS
        ):
            raise ValueError(f"{label}.timeout_seconds is invalid")
        env = _string_mapping(item.get("env"), f"{label}.env", env_names=True)
        secret_env = _string_mapping(
            item.get("secret_env"), f"{label}.secret_env", env_names=True
        )
        if any(not _ENV_NAME.fullmatch(name) for name in secret_env.values()):
            raise ValueError(f"{label}.secret_env contains an invalid secret name")
        if set(env) & set(secret_env):
            raise ValueError(f"{label} configures an environment name twice")
        names.add(name)
        result.append(
            ScriptToolSpec(
                name=name,
                description=" ".join(description.split()),
                runtime=runtime,
                entrypoint=entry.as_posix(),
                args_schema=MappingProxyType(
                    _validate_args_schema(
                        item.get("args_schema"), f"{label}.args_schema"
                    )
                ),
                timeout_seconds=timeout,
                effect=effect,
                directory=f"tools/scripts/{directory.name}",
                env=MappingProxyType(env),
                secret_env=MappingProxyType(secret_env),
            )
        )
    return tuple(result)


def _parse_mcp(files: Mapping[str, str]) -> tuple[MCPBundleServer, ...]:
    content = files.get("tools/mcp/servers.yaml")
    if content is None:
        return ()
    root = _load_yaml(content, "MCP configuration")
    if set(root) - {"version", "servers"} or root.get("version") != 1:
        raise ValueError("MCP configuration must contain version 1 and servers")
    servers = _mapping(root.get("servers", {}), "MCP servers")
    result: list[MCPBundleServer] = []
    for server_id, raw in sorted(servers.items()):
        if not isinstance(server_id, str) or not _NAME.fullmatch(server_id):
            raise ValueError("MCP server ID is malformed")
        item = _mapping(raw, f"MCP server {server_id}")
        unknown = set(item) - {
            "connection",
            "tools",
            "secret_env",
            "headers",
            "secret_headers",
        }
        if unknown:
            raise ValueError(
                f"MCP server {server_id} has unknown fields: {sorted(unknown)}"
            )
        connection = _mapping(
            item.get("connection"), f"MCP server {server_id}.connection"
        )
        tools = item.get("tools")
        if (
            not connection
            or not isinstance(tools, list)
            or not tools
            or any(
                not isinstance(name, str) or not _NAME.fullmatch(name) for name in tools
            )
            or len(tools) != len(set(tools))
        ):
            raise ValueError(f"MCP server {server_id} configuration is incomplete")
        secret_env = _string_mapping(
            item.get("secret_env"), f"MCP server {server_id}.secret_env", env_names=True
        )
        if any(not _ENV_NAME.fullmatch(name) for name in secret_env.values()):
            raise ValueError(
                f"MCP server {server_id}.secret_env contains an invalid secret name"
            )
        if secret_env and connection.get("transport") != "stdio":
            raise ValueError("MCP secret_env is supported only for local stdio servers")
        fixed_env = connection.get("env", {})
        if fixed_env:
            fixed_env = _string_mapping(
                fixed_env, f"MCP server {server_id}.connection.env", env_names=True
            )
            if set(fixed_env) & set(secret_env):
                raise ValueError(
                    f"MCP server {server_id} configures an environment name twice"
                )
            connection = {**connection, "env": fixed_env}
        connection_headers = _header_mapping(
            connection.get("headers"),
            f"MCP server {server_id}.connection.headers",
        )
        headers = _header_mapping(
            item.get("headers"), f"MCP server {server_id}.headers"
        )
        if _header_collision(connection_headers, headers):
            raise ValueError(f"MCP server {server_id} configures an HTTP header twice")
        fixed_headers = {**connection_headers, **headers}
        secret_headers = _header_mapping(
            item.get("secret_headers"), f"MCP server {server_id}.secret_headers"
        )
        if any(not _ENV_NAME.fullmatch(name) for name in secret_headers.values()):
            raise ValueError(
                f"MCP server {server_id}.secret_headers contains an invalid secret name"
            )
        if secret_headers and connection.get("transport") not in REMOTE_MCP_TRANSPORTS:
            raise ValueError(
                "MCP secret_headers requires an HTTP-based remote transport"
            )
        if secret_headers and not str(connection.get("url", "")).startswith("https://"):
            raise ValueError("secret-authenticated remote MCP URLs must use HTTPS")
        if _header_collision(fixed_headers, secret_headers):
            raise ValueError(f"MCP server {server_id} configures an HTTP header twice")
        if fixed_headers:
            connection = {**connection, "headers": fixed_headers}
        result.append(
            MCPBundleServer(
                server_id=server_id,
                connection=MappingProxyType(dict(connection)),
                tools=tuple(tools),
                secret_env=MappingProxyType(secret_env),
                secret_headers=MappingProxyType(secret_headers),
            )
        )
    return tuple(result)


def validate_repo_bundle(path: str | Path) -> ValidatedRepoBundle:
    """Validate one complete operator-selected bundle without installing it."""
    root = Path(path).expanduser().resolve(strict=True)
    files = _read_bundle_files(root)
    workflow_content = files.get("workflow.yaml")
    if workflow_content is None:
        raise ValueError("bundle workflow.yaml is required")
    skills = _parse_skills(root, files)
    scripts = _parse_scripts(root, files)
    mcp_servers = _parse_mcp(files)
    script_names = {item.name for item in scripts}
    mcp_names = {
        f"{server.server_id}_{tool}" for server in mcp_servers for tool in server.tools
    }
    collisions = (
        (script_names & BUILTIN_WORKFLOW_TOOLS)
        | (mcp_names & BUILTIN_WORKFLOW_TOOLS)
        | (script_names & mcp_names)
    )
    if collisions:
        raise ValueError(f"tool name collision: {sorted(collisions)}")
    workflow_document = _load_yaml(workflow_content, "workflow")
    known_tools = set(BUILTIN_WORKFLOW_TOOLS) | script_names | mcp_names
    skill_names = {item.name for item in skills}
    workflow = parse_workflow_spec(
        workflow_document,
        known_tools=known_tools,
        skill_exists=skill_names.__contains__,
    )
    effects = {item.name: item.effect for item in scripts}
    for task in workflow.tasks:
        for phase_name, phase in (
            ("planning", task.planning),
            ("validation", task.validation),
        ):
            forbidden = sorted(
                name for name in phase.tools if effects.get(name) == "mutate"
            )
            if forbidden:
                raise ValueError(
                    f"task {task.id} {phase_name} references mutating script "
                    f"tools: {forbidden}"
                )
    manifest = {
        "workflow": workflow.canonical_document(),
        "skills": [
            {"name": item.name, "description": item.description, "path": item.path}
            for item in skills
        ],
        "scripts": [
            {
                "name": item.name,
                "description": item.description,
                "runtime": item.runtime,
                "entrypoint": item.entrypoint,
                "args_schema": dict(item.args_schema),
                "timeout_seconds": item.timeout_seconds,
                "effect": item.effect,
                "directory": item.directory,
                "env": dict(item.env),
                "secret_env": dict(item.secret_env),
            }
            for item in scripts
        ],
        "mcp": [
            {
                "server_id": item.server_id,
                "connection": dict(item.connection),
                "tools": list(item.tools),
                "secret_env": dict(item.secret_env),
                "secret_headers": dict(item.secret_headers),
            }
            for item in mcp_servers
        ],
        "files": dict(sorted(files.items())),
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return ValidatedRepoBundle(
        root=root,
        digest=digest,
        workflow=workflow,
        workflow_document=MappingProxyType(workflow_document),
        skills=skills,
        scripts=scripts,
        mcp_servers=mcp_servers,
        files=MappingProxyType(files),
        manifest=MappingProxyType(manifest),
    )


class RepoConfigRegistry:
    """Durable trusted registry over the existing serialized state database."""

    def __init__(self, store: SQLiteGitHubStore) -> None:
        self.store = store

    def install(
        self, repo_id: int, bundle_path: str | Path, *, now: str | None = None
    ) -> RepoConfigGeneration:
        bundle = validate_repo_bundle(bundle_path)
        installed_at = now or datetime.now(UTC).isoformat()
        with self.store.transaction(immediate=True) as db:
            repo = db.execute(
                "SELECT 1 FROM repositories WHERE repo_id=?", (repo_id,)
            ).fetchone()
            if repo is None:
                raise ValueError("repository has not been observed")
            row = db.execute(
                "SELECT COALESCE(MAX(generation),0)+1 AS value "
                "FROM repo_config_generations_v1 WHERE repo_id=?",
                (repo_id,),
            ).fetchone()
            generation = int(row["value"])
            generation_id = hashlib.sha256(
                f"{repo_id}\0{generation}\0{bundle.digest}".encode()
            ).hexdigest()
            db.execute(
                """INSERT INTO repo_config_generations_v1(
                   generation_id,repo_id,generation,digest,workflow_id,
                   workflow_version,workflow_json,manifest_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    repo_id,
                    generation,
                    bundle.digest,
                    bundle.workflow.workflow_id,
                    bundle.workflow.version,
                    json.dumps(
                        bundle.workflow.canonical_document(),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        dict(bundle.manifest),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    installed_at,
                ),
            )
            db.executemany(
                """INSERT INTO repo_config_files_v1(generation_id,path,content,digest)
                   VALUES(?,?,?,?)""",
                [
                    (
                        generation_id,
                        path,
                        content,
                        hashlib.sha256(content.encode()).hexdigest(),
                    )
                    for path, content in sorted(bundle.files.items())
                ],
            )
            db.execute(
                """INSERT INTO repo_config_current_v1(repo_id,generation_id,updated_at)
                   VALUES(?,?,?) ON CONFLICT(repo_id) DO UPDATE SET
                   generation_id=excluded.generation_id,updated_at=excluded.updated_at""",
                (repo_id, generation_id, installed_at),
            )
        return self.generation(repo_id, generation_id)

    def generation(self, repo_id: int, generation_id: str) -> RepoConfigGeneration:
        row = self.store.connection.execute(
            "SELECT * FROM repo_config_generations_v1 "
            "WHERE repo_id=? AND generation_id=?",
            (repo_id, generation_id),
        ).fetchone()
        if row is None:
            raise PermissionError("repository configuration generation is unavailable")
        return RepoConfigGeneration(
            generation_id=row["generation_id"],
            repo_id=int(row["repo_id"]),
            generation=int(row["generation"]),
            digest=row["digest"],
            workflow_id=row["workflow_id"],
            workflow_version=int(row["workflow_version"]),
            created_at=row["created_at"],
            manifest=MappingProxyType(json.loads(row["manifest_json"])),
        )

    def current_generation(self, repo_id: int) -> RepoConfigGeneration | None:
        row = self.store.connection.execute(
            "SELECT generation_id FROM repo_config_current_v1 WHERE repo_id=?",
            (repo_id,),
        ).fetchone()
        return self.generation(repo_id, row["generation_id"]) if row else None

    def thread_generation(self, thread_id: str) -> RepoConfigGeneration | None:
        row = self.store.connection.execute(
            "SELECT repo_id,config_generation_id FROM issue_threads WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise ValueError("IssueThread does not exist")
        generation_id = row["config_generation_id"]
        return (
            self.generation(int(row["repo_id"]), generation_id)
            if generation_id
            else None
        )

    def _file(self, repo_id: int, generation_id: str, path: str) -> str | None:
        self.generation(repo_id, generation_id)
        row = self.store.connection.execute(
            "SELECT content FROM repo_config_files_v1 WHERE generation_id=? AND path=?",
            (generation_id, path),
        ).fetchone()
        return str(row["content"]) if row else None

    def load_workflow(self, repo_id: int, generation_id: str) -> WorkflowSpec:
        generation = self.generation(repo_id, generation_id)
        manifest = generation.manifest
        scripts = {item["name"] for item in manifest["scripts"]}
        mcp = {
            f"{server['server_id']}_{name}"
            for server in manifest["mcp"]
            for name in server["tools"]
        }
        skills = {item["name"] for item in manifest["skills"]}
        return parse_workflow_spec(
            manifest["workflow"],
            known_tools=set(BUILTIN_WORKFLOW_TOOLS) | scripts | mcp,
            skill_exists=skills.__contains__,
        )

    def read_skill(self, repo_id: int, generation_id: str, name: str) -> str:
        content = self._file(repo_id, generation_id, f"skills/{name}/SKILL.md")
        if content is None:
            raise PermissionError("required bound-generation skill is missing")
        return content

    def skill_files(self, repo_id: int, generation_id: str) -> dict[str, str]:
        self.generation(repo_id, generation_id)
        rows = self.store.connection.execute(
            "SELECT path,content FROM repo_config_files_v1 "
            "WHERE generation_id=? AND path LIKE 'skills/%' ORDER BY path",
            (generation_id,),
        ).fetchall()
        return {row["path"][len("skills/") :]: row["content"] for row in rows}

    def script_specs(
        self, repo_id: int, generation_id: str
    ) -> tuple[ScriptToolSpec, ...]:
        manifest = self.generation(repo_id, generation_id).manifest
        return tuple(
            ScriptToolSpec(
                name=item["name"],
                description=item["description"],
                runtime=item["runtime"],
                entrypoint=item["entrypoint"],
                args_schema=MappingProxyType(item["args_schema"]),
                timeout_seconds=int(item["timeout_seconds"]),
                effect=item["effect"],
                directory=item["directory"],
                env=MappingProxyType(item.get("env", {})),
                secret_env=MappingProxyType(item.get("secret_env", {})),
            )
            for item in manifest["scripts"]
        )

    def script_files(
        self, repo_id: int, generation_id: str, spec: ScriptToolSpec
    ) -> dict[str, str]:
        self.generation(repo_id, generation_id)
        prefix = spec.directory + "/"
        rows = self.store.connection.execute(
            "SELECT path,content FROM repo_config_files_v1 "
            "WHERE generation_id=? AND path LIKE ? ORDER BY path",
            (generation_id, prefix + "%"),
        ).fetchall()
        return {row["path"][len(prefix) :]: row["content"] for row in rows}

    def capability_registry(
        self, repo_id: int, generation_id: str
    ) -> RepoCapabilityRegistry | None:
        manifest = self.generation(repo_id, generation_id).manifest
        if not manifest["mcp"]:
            return None
        registry = RepoCapabilityRegistry()
        for item in manifest["mcp"]:
            registry.register_server(
                MCPServerSpec(
                    item["server_id"],
                    dict(item["connection"]),
                    dict(item.get("secret_env", {})),
                    dict(item.get("secret_headers", {})),
                )
            )
            registry.approve(repo_id, item["server_id"], set(item["tools"]))
        return registry

    def safe_summary(self, repo_id: int) -> dict[str, Any]:
        generation = self.current_generation(repo_id)
        if generation is None:
            raise ValueError("repository has no installed configuration")
        manifest = generation.manifest
        return {
            "generation": generation.generation,
            "generation_id": generation.generation_id,
            "digest": generation.digest,
            "workflow": {
                "id": generation.workflow_id,
                "version": generation.workflow_version,
                "tasks": [item["id"] for item in manifest["workflow"]["tasks"]],
            },
            "skills": [
                {"name": item["name"], "description": item["description"]}
                for item in manifest["skills"]
            ],
            "scripts": [
                {
                    "name": item["name"],
                    "effect": item["effect"],
                    "credentials": self._credential_status(
                        repo_id, item.get("secret_env", {})
                    ),
                }
                for item in manifest["scripts"]
            ],
            "mcp": [
                {
                    "server": item["server_id"],
                    "tools": item["tools"],
                    "credentials": self._credential_status(
                        repo_id,
                        {
                            **item.get("secret_env", {}),
                            **item.get("secret_headers", {}),
                        },
                    ),
                }
                for item in manifest["mcp"]
            ],
        }

    def _credential_status(
        self, repo_id: int, references: Mapping[str, str]
    ) -> dict[str, int]:
        required = set(references.values())
        if not required:
            return {"required": 0, "configured": 0}
        placeholders = ",".join("?" for _ in required)
        row = self.store.connection.execute(
            f"SELECT count(*) AS total FROM repo_secrets_v1 "
            f"WHERE repo_id=? AND name IN ({placeholders})",
            (repo_id, *sorted(required)),
        ).fetchone()
        return {"required": len(required), "configured": int(row["total"])}
