"""Work grants: where a delegated A2A task is allowed to work.

A caller hands work over with ``delegate_task``; a grant says *where* that work
happens — which directory the peer treats as its workspace for the task, and
which files or directories outside it the peer may also read and write. The grant
travels in the A2A message metadata under namespaced keys, so a peer that does
not understand it simply ignores it and runs in its own workspace.

A grant is a **request, never an authorization**, and is checked on both sides:

1. The caller may only hand out what it can reach itself
   (:meth:`GrantPolicy.authorize` over its own writable roots), so an agent
   cannot widen its own reach by delegating.
2. The server authorizes the incoming request against *its* own configuration
   and refuses anything outside it with ``-32602 InvalidParams``.

A server with no policy refuses every grant and works only in its own workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from little_agent.config import AgentConfig
from little_agent.paths import is_within

# Metadata keys carrying the grant between Little Agent peers.
WORKSPACE_METADATA_KEY = "littleAgent/workspace"
ALLOWED_PATHS_METADATA_KEY = "littleAgent/allowedPaths"


class GrantError(ValueError):
    """A requested workspace or path that may not be handed out or accepted."""


def _clean(value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    return Path(text).expanduser()


@dataclass(frozen=True, slots=True)
class WorkGrant:
    """Where a delegated task should work, as requested by the caller.

    ``allowed_paths`` are granted for read *and* write: they are the deliberate
    exception to "everything happens inside the workspace", and a peer asked to
    produce something outside its workspace needs both.
    """

    workspace: Path | None = None
    allowed_paths: tuple[Path, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.workspace is None and not self.allowed_paths

    def to_metadata(self) -> dict[str, Any]:
        """The metadata entries to attach to an A2A message (empty when unset)."""

        metadata: dict[str, Any] = {}
        if self.workspace is not None:
            metadata[WORKSPACE_METADATA_KEY] = str(self.workspace)
        if self.allowed_paths:
            metadata[ALLOWED_PATHS_METADATA_KEY] = [str(path) for path in self.allowed_paths]
        return metadata

    @classmethod
    def from_metadata(cls, metadata: Any) -> "WorkGrant":
        """Read a grant off an incoming message; anything malformed is ignored."""

        if not isinstance(metadata, dict):
            return cls()
        workspace = _clean(metadata.get(WORKSPACE_METADATA_KEY))
        raw_paths = metadata.get(ALLOWED_PATHS_METADATA_KEY)
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        paths: list[Path] = []
        if isinstance(raw_paths, list):
            for entry in raw_paths:
                path = _clean(entry)
                if path is not None and path not in paths:
                    paths.append(path)
        return cls(workspace=workspace, allowed_paths=tuple(paths))

    def resolve(self, base: Path) -> "WorkGrant":
        """Absolute form of this grant, taking relative entries from ``base``."""

        def absolute(path: Path) -> Path:
            return (path if path.is_absolute() else base / path).resolve()

        return WorkGrant(
            workspace=absolute(self.workspace) if self.workspace is not None else None,
            allowed_paths=tuple(dict.fromkeys(absolute(path) for path in self.allowed_paths)),
        )

    @classmethod
    def request(
        cls,
        base: Path,
        workspace: Any = None,
        allowed_paths: Any = None,
    ) -> "WorkGrant":
        """Build a grant from raw tool arguments, resolved against ``base``.

        Raises :class:`GrantError` when ``allowed_paths`` is not a list of paths.
        """

        if isinstance(allowed_paths, str):
            allowed_paths = [allowed_paths]
        if allowed_paths is None:
            allowed_paths = []
        if not isinstance(allowed_paths, list):
            raise GrantError("allowed_paths must be an array of file or directory paths.")
        paths: list[Path] = []
        for entry in allowed_paths:
            path = _clean(entry)
            if path is None:
                raise GrantError("allowed_paths entries must be non-empty paths.")
            paths.append(path)
        return cls(workspace=_clean(workspace), allowed_paths=tuple(paths)).resolve(base)

    def apply(self, config: AgentConfig) -> AgentConfig:
        """Layer the grant onto a config: each field only when the grant sets it.

        The workspace replaces ``config.workspace`` (so relative tool paths follow
        the delegated directory), and the granted paths replace
        ``config.writable_paths`` — which the path policy also counts as readable.
        """

        if self.is_empty:
            return config
        return replace(
            config,
            workspace=self.workspace.resolve() if self.workspace is not None else config.workspace,
            writable_paths=(
                tuple(path.resolve() for path in self.allowed_paths)
                if self.allowed_paths
                else config.writable_paths
            ),
        )


@dataclass(frozen=True, slots=True)
class GrantPolicy:
    """Decides which requested paths one side is willing to hand out or accept.

    ``roots`` is what the deciding agent can itself reach and write. ``allow_any``
    lifts the check for a deliberately open, private server.
    """

    roots: tuple[Path, ...] = ()
    allow_any: bool = False

    @classmethod
    def from_config(cls, config: AgentConfig, allow_any: bool = False) -> "GrantPolicy":
        # Writable roots only: a grant conveys read *and* write, so a path this
        # agent may merely read is not its to hand on.
        roots = (config.workspace.resolve(), *(path.resolve() for path in config.writable_paths))
        return cls(roots=tuple(dict.fromkeys(roots)), allow_any=allow_any)

    def _check(self, label: str, path: Path) -> Path:
        resolved = path.expanduser().resolve()
        if self.allow_any:
            return resolved
        if any(is_within(root, resolved) for root in self.roots):
            return resolved
        known = ", ".join(str(root) for root in self.roots) or "(none)"
        raise GrantError(
            f"{label} is outside the accessible paths: {path} (allowed roots: {known})"
        )

    def authorize(self, grant: WorkGrant) -> WorkGrant:
        """Return the grant with absolute paths, or raise :class:`GrantError`."""

        if grant.is_empty:
            return grant
        workspace = (
            self._check("workspace", grant.workspace) if grant.workspace is not None else None
        )
        paths = tuple(self._check("allowed path", path) for path in grant.allowed_paths)
        return WorkGrant(workspace=workspace, allowed_paths=paths)
