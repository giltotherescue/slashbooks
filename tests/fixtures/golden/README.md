# Golden outputs

Expected outputs for three synthetic companies, built from scratch by
`tests/golden_companies.py` and compared by `tests/test_golden_outputs.py`:

- `cash/` cash-basis company: three months of bank and card activity, a split,
  card-payment transfers, a reconciliation and a closed month.
- `accrual/` native accrual company: opening balances, invoices, bills, payments,
  an applied credit, prepaid, deferred, depreciation, an auto-reversing estimate,
  a true-up, a write-off, a saved rule and a closed month.
- `quickbooks/` accrual company migrated from the QuickBooks acceptance fixture
  (`tests/fixtures/accrual/acceptance/`).
- `shared/` company-independent command contracts (`command_spec`, schemas).

Each directory holds every user-visible output as readable text or JSON:
statements (text and JSON), accrual reports, readiness per month, the export
package (CSV and per-sheet text of the workbook), tax map and export, `books ask`
answers, queue views, routine plan, ledger digest and meta, accrual records,
audit events, company files, and the migration bundle manifest. `MANIFEST.json`
lists the command, exit code and stdout hash of each captured command.

The clock is frozen and `uuid.uuid4()` is a counter, so nothing is rewritten
except the temp directory path (see `tests/golden_support.py`).

A diff here means a user-visible change. Regenerate only on purpose:

    UPDATE_GOLDEN=1 .venv/bin/python -m unittest tests.test_golden_outputs

Regenerating requires a written reason in the pull request.

2026-10-19 review corrections: regenerated the affected expectations because
confirmation receipts now persist the learning choice, so audit hashes and the
learned-context operation hashes change while confirmation counts and financial
amounts stay the same. Tax handoffs now identify their actual recording basis
and include accounting coverage: accrual exports no longer claim cash basis.
The cash manifest also records its cash basis explicitly. These are intended
audit and export-description changes, with unchanged statement amounts. Carry
this reason into the pull request when publishing these local fixes.
