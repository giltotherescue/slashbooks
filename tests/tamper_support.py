"""Helper for tests that simulate tampering with a ledger store.

The store has append-only triggers that refuse an UPDATE or DELETE of posted rows. A person with direct
access to the database file can drop them, so a test that simulates that person drops them first.
"""
from __future__ import annotations

import sqlite3


def drop_append_only_guard(conn: sqlite3.Connection) -> sqlite3.Connection:
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        conn.execute(f'DROP TRIGGER "{name}"')
    return conn
