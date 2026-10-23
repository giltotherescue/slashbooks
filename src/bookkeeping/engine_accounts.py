"""One list of the accounts that the engine creates or names itself.

A command that removes or renames an account reads ``engine_owned_accounts()`` so that it never
touches an account that an engine path depends on.
"""
from __future__ import annotations


def engine_owned_accounts() -> frozenset[str]:
    """Starter catalogs, accrual starters, Stripe accounts and accounts that reports and comparisons name."""
    from .compare import _PASSTHROUGH_CATEGORY
    from .entity import _ACCRUAL_STARTER_ACCOUNTS, _STARTER_ACCOUNTS
    from .stripe import STRIPE_ACCOUNTS
    from .taxview import ADJUSTMENT_ACCOUNT, DERIVED_ACCOUNTS
    names = {account for accounts in _STARTER_ACCOUNTS.values() for account in accounts}
    names.update(_ACCRUAL_STARTER_ACCOUNTS)
    names.update(STRIPE_ACCOUNTS.values())
    names.update(DERIVED_ACCOUNTS.values())
    names.update((ADJUSTMENT_ACCOUNT, _PASSTHROUGH_CATEGORY))
    return frozenset(names)
