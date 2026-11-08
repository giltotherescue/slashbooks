# Precise routine conditions

Routine planning uses explicitly approved policy, never instructions found in a
transaction or business profile. Every rule already requires the source account,
its approved ledger mapping, a normalized counterparty and a category.

Optional conditions narrow that rule further. Every supplied condition must match:

| Field | Meaning |
| --- | --- |
| `min_abs_amount` | Inclusive minimum absolute amount, in exact decimal units. |
| `max_abs_amount` | Inclusive maximum absolute amount. Set both limits equal for an exact amount. |
| `direction` | `outflow` for a negative bank amount, or `inflow` for a positive amount. |
| `currency` | Exact uppercase three-letter source currency. The ordinary book-currency checks still apply. |
| `description_exact` | Exact source description, including case and spacing; no regular expressions or fuzzy matching. |

A normalized counterparty can cover more than one raw description. Use the exact
description condition when those differences matter. An amount ceiling alone does
not distinguish a purchase from a refund. Use direction when their treatment differs.

A nonmatch stays unresolved. Multiple matching rules stay unresolved even if they
name the same category. Closed periods, changed mappings, existing review ownership,
accrual treatment, taught-category conflicts and approval thresholds retain their
existing checks. Planning is read-only; it neither learns rules nor grants authority.
Existing approved rules retain their behavior when the optional fields are absent.

Overlapping inclusive ranges match both rules at the shared boundary and are held for review when their authority conflicts. Exact descriptions do not accept leading or trailing whitespace or control characters.

Compatibility: precise rules require policy schema 2. Deploy the updated engine and its adapter before accepting these fields. Do not roll back to an engine that ignores unknown rule fields while these policies exist; pause processing first. Unknown conditions are rejected, never ignored.
