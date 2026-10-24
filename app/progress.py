"""Structured progress events for a running investigation.

Agents call emit(kind, **data) at each meaningful step. Every listener active in the
current context receives the event: the web UI's timeline and the run's JSONL trace
(logs/trace-<run_id>.jsonl). Outside listen() nothing happens. Like careops_world, the
listeners live in a context variable, so tools called deep inside the agents reach them.

Event kinds: run_started, attempt_started, data_request, query, query_failed, world_model,
llm_call, investigator_done, reminder, code_checks, critic_started, verdict, run_finished.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Callable, Iterator

_listeners: ContextVar[tuple[Callable[[dict], None], ...]] = ContextVar("careops_progress", default=())
_run_id: ContextVar[str | None] = ContextVar("careops_run_id", default=None)


def emit(kind: str, **data) -> None:
        event = None
        for listener in _listeners.get():
                event = event or {"kind": kind, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **data}
                listener(event)


@contextmanager
def listen(fn: Callable[[dict], None]) -> Iterator[None]:
        token = _listeners.set(_listeners.get() + (fn,))
        try:
                yield
        finally:
                _listeners.reset(token)


def current_run_id() -> str | None:
        """The run in progress in this context (used to tag log lines with run_id)."""
        return _run_id.get()


@contextmanager
def run_scope(run_id: str) -> Iterator[None]:
        token = _run_id.set(run_id)
        try:
                yield
        finally:
                _run_id.reset(token)
