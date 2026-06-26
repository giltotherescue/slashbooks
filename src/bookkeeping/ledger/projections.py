from __future__ import annotations

"""Projection helpers for rendering store-backed ledger outputs."""

from pathlib import Path
import sqlite3

from .store import LedgerStore
from .writer import render_ledger


def render_store_ledger(store_path: Path | str, conn: sqlite3.Connection | None = None) -> str:
    """Render a deterministic Beancount ledger from a SQLite store."""
    store = LedgerStore(store_path)
    if conn is None:
        title = store.get_meta("title") or "Books"
    else:
        row = conn.execute("SELECT value FROM meta WHERE key = 'title'").fetchone()
        title = str(row[0]) if row else "Books"
    return render_ledger(
        opens=store.load_opens(conn),
        entries=store.load_entries(conn=conn),
        balances=store.load_balances(conn),
        title=title,
    )
