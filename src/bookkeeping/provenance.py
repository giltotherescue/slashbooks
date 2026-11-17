"""Who is calling the engine.

A host that lets an automated agent run engine commands must say so, so that
the engine does not record the agent's values as stated by a person. The kind
of caller is one of ``KINDS``:

- ``person``: a person decided or typed the value. This is the default when
  nothing is set, so existing callers and stored data do not change.
- ``agent``: an automated agent supplied the value.
- ``system``: an automatic job supplied the value.
- ``unknown``: the host set a value the engine does not know.

``caller_kind()`` reads, in this order, the value set by ``use_caller_kind``
for the current context, then the environment variable ``BOOKS_CALLER_KIND``,
then the default. A value that is set but not in ``KINDS`` is ``unknown``. A
host sets the context around a job and passes the environment variable to the
engine commands it starts.

The kind never comes from statement content or from command arguments, so an
imported file or an agent's own arguments cannot choose it. Only a person
decision counts as a confirmation: a caller-stated period from any other kind
is recorded with its source but not as a confirmed period.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

KINDS = frozenset({"person", "agent", "system", "unknown"})
DEFAULT_KIND = "person"
ENV_VAR = "BOOKS_CALLER_KIND"

_kind: ContextVar[str | None] = ContextVar("books_caller_kind", default=None)


def _normalize(value: object) -> str:
    return value if isinstance(value, str) and value in KINDS else "unknown"


def caller_kind() -> str:
    """The kind of the current caller: context, then environment, then ``person``."""
    explicit = _kind.get()
    if explicit is not None:
        return _normalize(explicit)
    env = os.environ.get(ENV_VAR)
    if env is not None and env.strip() != "":
        return _normalize(env.strip())
    return DEFAULT_KIND


@contextmanager
def use_caller_kind(kind: str) -> Iterator[str]:
    """Set the caller kind for the current context. It is reset on exit, also on failure."""
    token = _kind.set(_normalize(kind))
    try:
        yield caller_kind()
    finally:
        _kind.reset(token)


def is_person(kind: str | None = None) -> bool:
    """True when the caller (or the given kind) is a person."""
    return (caller_kind() if kind is None else kind) == "person"
