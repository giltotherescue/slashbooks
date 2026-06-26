from __future__ import annotations

"""Shared normalization primitive for transaction descriptions.

The normalized description is used as a component of the dedup correlation key
(account + amount + date + normalized description) for pending→posted supersession.
It is also used by the importer and comparison layers — defined once here so no
peer module depends on the importer's internals.

Contract (KTD):
    normalize_description(text) -> str
    - Uppercase
    - Collapse whitespace runs to a single space
    - Strip leading/trailing whitespace
    - Strip trailing reference/store-number digit runs of length ≥4
      (optionally preceded by '#' or '*', optionally preceded by whitespace)
"""

import re

# Pattern: optional whitespace, optional '#' or '*', then ≥4 digits, at end of string
_TRAILING_REF_RE = re.compile(r"\s*[#*]?\s*\d{4,}$")


def normalize_description(text: str) -> str:
    """Return a normalized form of *text* suitable for dedup correlation.

    >>> normalize_description("STARBUCKS #1234")
    'STARBUCKS'
    >>> normalize_description("amazon.com*5678")
    'AMAZON.COM'
    >>> normalize_description("  whole foods market  ")
    'WHOLE FOODS MARKET'
    >>> normalize_description("COFFEE SHOP 00012345")
    'COFFEE SHOP'
    """
    # Uppercase
    result = text.upper()
    # Collapse whitespace runs
    result = re.sub(r"\s+", " ", result)
    # Strip ends
    result = result.strip()
    # Strip trailing reference/store-number digit runs of length ≥4
    result = _TRAILING_REF_RE.sub("", result)
    # Strip again in case stripping the ref left trailing whitespace
    result = result.strip()
    return result
