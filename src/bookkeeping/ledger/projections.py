from __future__ import annotations

"""Projection helpers for rendering store-backed ledger outputs."""

from pathlib import Path

from .store import LedgerStore
from .writer import render_ledger


def render_store_ledger(store_path: Path | str) -> str:
    """Render a deterministic Beancount ledger from a SQLite store."""
    store = LedgerStore(store_path)
    title = store.get_meta("title") or "Books"
    return render_ledger(
        opens=store.load_opens(),
        entries=store.load_entries(),
        balances=store.load_balances(),
        title=title,
    )
