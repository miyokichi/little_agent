"""Chat: a conversation on top of the shared agent runtime.

The runtime keeps nothing between runs, so continuity lives here. A session owns
the transcript and hands it back to :meth:`Agent.run` each turn; that is the only
difference between a chat turn and an A2A task, which passes no history and so
starts clean every time.

Persistence is a separate axis, and it is the store that decides:

* ``chat``               -> :class:`~little_agent.memory.store.FileMemoryStore`
* ``chat --no-memory``   -> :class:`~little_agent.memory.store.NullMemoryStore`
* A2A                    -> :class:`~little_agent.memory.store.NullMemoryStore`

With memory off the transcript still works for the length of the session; nothing
is loaded at the start and nothing is written at the end.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from little_agent.agent import Agent, StructuredOutputError
from little_agent.messages import Message
from little_agent.paths import is_within

if TYPE_CHECKING:
    from little_agent.memory.store import MemoryStore


class ChatSession:
    """One interactive conversation with one agent.

    ``history`` holds the user/assistant turns only. Tool traffic belongs to the
    run that produced it and is not replayed: it would grow without bound and add
    little the final answer does not already carry.
    """

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.history: list[Message] = []
        self._active_skills: tuple[str, ...] = ()
        # How much of the transcript the last reflection saw, so ``learn`` is a
        # no-op when nothing has been said since.
        self._learned_upto = 0
        self._readable: tuple[Path, ...] = ()
        self._writable: tuple[Path, ...] = ()
        self.agent.directory_adder = self.add_directory

    @property
    def memory(self) -> "MemoryStore":
        return self.agent.memory

    def switch_agent(self, agent: Agent) -> None:
        """Continue this conversation with a different agent.

        The transcript carries over — the user is still talking about the same
        thing — but the reflection watermark does not reset, so a switch mid-way
        does not cause the earlier turns to be learned twice.
        """

        self.agent.directory_adder = None
        self.agent = agent
        self.agent.config = replace(
            agent.config,
            readable_paths=tuple(dict.fromkeys((*agent.config.readable_paths, *self._readable))),
            writable_paths=tuple(dict.fromkeys((*agent.config.writable_paths, *self._writable))),
        )
        self.agent.directory_adder = self.add_directory

    def add_directory(self, path: str, access: str = "write") -> Path:
        """Trusted host entry point; the LLM Tool always asks before calling it.

        Changes affect the current chat only. In particular, never modify the
        caller's frozen config, shared MCP server policy, or other sessions.
        """
        if not isinstance(path, str) or not path.strip():
            raise ValueError("ディレクトリのパスを指定してください。")
        if access not in {"read", "write"}:
            raise ValueError("access は read または write を指定してください。")
        directory = Path(path).expanduser()
        if not directory.is_absolute():
            directory = self.agent.config.workspace / directory
        directory = directory.resolve(strict=True)
        if not directory.is_dir():
            raise ValueError(f"ディレクトリではありません: {directory}")
        config = self.agent.config
        roots = (config.workspace, *config.writable_paths)
        if access == "read":
            roots += config.readable_paths
        if any(is_within(root.resolve(), directory) for root in roots):
            return directory
        if access == "read":
            self._readable = (*self._readable, directory)
            config = replace(config, readable_paths=(*config.readable_paths, directory))
        else:
            self._writable = (*self._writable, directory)
            config = replace(config, writable_paths=(*config.writable_paths, directory))
        self.agent.config = config
        return directory

    def send(self, text: str, on_progress: Callable[[str], None] | None = None) -> str:
        """Run one turn and return the answer, recording both sides."""

        try:
            result = self.agent.run(text, history=self.history, on_progress=on_progress,
                                    active_skills=self._active_skills)
        except StructuredOutputError as exc:
            return f"Structured output error: {exc}"
        answer = result.text
        self._active_skills = result.skills
        self.history.append(Message(role="user", content=text))
        self.history.append(Message(role="assistant", content=answer))
        return answer

    def clear(self) -> int:
        """Drop the transcript and start fresh. Returns how many turns went."""

        dropped = len(self.history)
        self.history.clear()
        self._learned_upto = 0
        self._active_skills = ()
        return dropped

    def learn(self) -> bool:
        """Distil the conversation so far into durable memory.

        Called at the end of a session and by ``/remember``. Returns True when
        something was actually written, which is never the case with memory off.
        """

        if len(self.history) <= self._learned_upto:
            return False
        # Mark progress even when the store declines, so a no-op is not retried.
        learned = self.memory.learn(self.history, self.agent.llm, self.agent.config.model)
        self._learned_upto = len(self.history)
        if learned and self.agent.logger:
            self.agent.logger.log_conversation("auto_learning", turns=len(self.history))
        return learned
