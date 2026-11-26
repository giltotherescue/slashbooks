"""One list of the accounts that the engine creates or names itself.

A command that removes or renames an account reads ``engine_owned_accounts()`` so that it never
touches an account that an engine path depends on.
"""
from __future__ import annotations


def engine_owned_accounts() -> frozenset[str]:
    """Starter catalogs, accrual starters, Stripe accounts and accounts that reports and comparisons name."""
    from .compare import _PASSTHROUGH_CATEGORY
    from .entity import _ACCRUAL_STARTER_ACCOUNTS, _STARTER_ACCOUNTS
    from .stripe import STRIPE_ACCOUNTS, STRIPE_HOLDING_ACCOUNT
    from .taxview import ADJUSTMENT_ACCOUNT, DERIVED_ACCOUNTS
    names = {account for accounts in _STARTER_ACCOUNTS.values() for account in accounts}
    names.update(_ACCRUAL_STARTER_ACCOUNTS)
    names.update(STRIPE_ACCOUNTS.values())
    names.add(STRIPE_HOLDING_ACCOUNT)
    names.update(DERIVED_ACCOUNTS.values())
    names.update((ADJUSTMENT_ACCOUNT, _PASSTHROUGH_CATEGORY))
    return frozenset(names)


def rename_protected_accounts() -> frozenset[str]:
    """The engine accounts that keep their names: ``engine_owned_accounts()`` less the free starter categories.

    A starter income or expense category (for example ``Expenses:Office``) is the company's own category after
    the company starts: no engine path names it, so ``books entity account-rename`` can give it a new name. A
    starter balance sheet or equity account (``Assets:Bank:Checking``, ``Liabilities:CreditCard``,
    ``Equity:Opening-Balances``) stays, because engine code reads those names. A starter category that another
    engine list also names (accrual, Stripe, tax view, comparison) stays too.
    """
    from .compare import _PASSTHROUGH_CATEGORY
    from .entity import _ACCRUAL_STARTER_ACCOUNTS, _STARTER_ACCOUNTS
    from .stripe import STRIPE_ACCOUNTS, STRIPE_HOLDING_ACCOUNT
    from .taxview import ADJUSTMENT_ACCOUNT, DERIVED_ACCOUNTS
    named = set(_ACCRUAL_STARTER_ACCOUNTS) | set(STRIPE_ACCOUNTS.values()) | set(DERIVED_ACCOUNTS.values())
    named.add(STRIPE_HOLDING_ACCOUNT)
    named.update((ADJUSTMENT_ACCOUNT, _PASSTHROUGH_CATEGORY))
    free = {account for accounts in _STARTER_ACCOUNTS.values() for account in accounts
            if account.split(":")[0] in ("Income", "Expenses") and account not in named}
    return frozenset(engine_owned_accounts() - free)
