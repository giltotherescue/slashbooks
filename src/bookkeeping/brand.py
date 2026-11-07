"""The product name shown in user-visible output.

The open-source project calls itself "Slashbooks". A host that offers the engine
under its own name chooses the name for each run, in this order:

1. ``with use_product_name("Example Books"):`` around in-process engine calls.
   The value lives in a ``contextvars.ContextVar``. It applies only to the code
   inside the block, in the current thread or task, and is reset when the block
   ends, so a long-lived process never leaks a name into the next run.
2. ``books --product-name "Example Books" ...`` for one CLI run.
3. The environment variable ``SLASHBOOKS_PRODUCT_NAME`` for a process that runs
   one job, such as a container that starts the CLI.
4. The default, "Slashbooks".

The name appears only as text. It never changes a number, a file name, a key in
a JSON document or a protocol value.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

ENV_VAR = "SLASHBOOKS_PRODUCT_NAME"
DEFAULT_NAME = "Slashbooks"
MAX_NAME_CHARS = 40
# Characters that make a spreadsheet program read a cell as a formula.
_FORMULA_STARTS = ("=", "+", "-", "@")

_run_name: ContextVar[str | None] = ContextVar("slashbooks_product_name", default=None)


class ProductNameError(ValueError):
    """The configured product name is not safe to print."""


def validate_product_name(value: str) -> str:
    """Return *value* trimmed, or raise ProductNameError.

    A name is 1 to 40 printable characters on one line. It cannot start with
    ``=``, ``+``, ``-`` or ``@``, because a CSV or XLSX cell would run as a formula.
    """
    if not isinstance(value, str):
        raise ProductNameError("The product name must be text.")
    name = value.strip()
    if not name:
        raise ProductNameError("The product name is empty.")
    if len(name) > MAX_NAME_CHARS:
        raise ProductNameError(f"The product name is longer than {MAX_NAME_CHARS} characters.")
    if not name.isprintable():
        raise ProductNameError("The product name has a control, newline or tab character.")
    if name.startswith(_FORMULA_STARTS):
        raise ProductNameError("The product name cannot start with =, +, - or @.")
    return name


@contextmanager
def use_product_name(name: str | None) -> Iterator[None]:
    """Use *name* for engine output inside the block. ``None`` keeps the current choice.

    An invalid name raises ProductNameError before the block runs.
    """
    if name is None:
        yield
        return
    token = _run_name.set(validate_product_name(name))
    try:
        yield
    finally:
        _run_name.reset(token)


def product_name() -> str:
    """The product name for this run. The default is "Slashbooks".

    The run value wins over the environment variable. An unset or blank variable
    gives the default. An invalid variable raises ProductNameError, so a host
    finds a bad setting at once instead of printing it.
    """
    chosen = _run_name.get()
    if chosen is not None:
        return chosen
    raw = os.environ.get(ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_NAME
    try:
        return validate_product_name(raw)
    except ProductNameError as exc:
        raise ProductNameError(f"{ENV_VAR} is invalid: {exc}") from None


def is_default_name() -> bool:
    return product_name() == DEFAULT_NAME
