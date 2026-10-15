"""Slashbooks agent conformance suite.

Runs an AI agent that has the Slashbooks skills against a synthetic company
with seeded traps, then scores the recorded actions. The suite uses only the
Python standard library.
"""

__all__ = ["world", "scenarios", "scoring", "runner"]
