# Company currency

Each company keeps its books in one base currency. It does not have to be USD.
Slashbooks does not convert between currencies, keep exchange rates or compute
exchange gains and losses. Every import, entry, report and export of the company
uses its one currency.

## Where the setting is

Two keys in `entity.json`:

- `currency`: an ISO 4217 code such as `USD`, `EUR`, `GBP` or `CAD`. When the key
  is absent, the company currency is `USD`. A company made before this setting
  existed is therefore unchanged, and its outputs are byte-identical.
- `currency_source`: `default` (absent also means default), `inferred` (the
  first import that carried a currency set it) or `set` (a person ran
  `books entity set-currency`).

When the currency changes, the ledger records it in its `meta` table (`currency`
and `currency_source`) in the same transaction as the audit event, and new accounts
open in it. The ledger commits first and entity.json follows. If a run stops in
between, a read of the company uses the ledger currency in memory and writes
nothing; the next write command repairs entity.json under the company write lock.
`books entity account-add --currency CODE` still accepts another code for an
account, but no import or posting can use it: the ledger writer refuses a posting
that is not in the company currency.

## Supported currencies

This version keeps books only in currencies with two decimal places, such as
USD, EUR, GBP, CAD, AUD, CHF, SEK or MXN. The engine rounds money to cents in many
places, so a currency with zero decimal places (JPY, KRW, CLP) or three decimal
places (KWD, BHD, OMR) is refused with the error code
`unsupported_currency_decimals`. It is not rounded silently. The built-in table
in `src/bookkeeping/currency.py` lists each known code with its decimal places and
display symbol.

## Set the currency

A person sets the currency while the company has no entries at all: no ledger
entries, no balance assertions, no imported rows, no review queue items and no
accrual records.

```sh
books entity set-currency ~/Documents/books/acme --currency EUR
books entity set-currency ~/Documents/books/acme --currency EUR --apply --plan-hash <plan_hash>
```

Without `--apply` the command shows a preview and writes nothing. With `--apply`
it writes `currency` and `currency_source: "set"` to `entity.json`, moves the
account catalog to the new currency and adds the audit event `currency-set`.
Repeating the same value returns `already_applied`. `--currency USD` on a company
that is USD by default records `currency_source: "set"`, so a later import in
another currency is refused instead of adopted. A recorded USD and an absent key
give the same outputs. The command is person-only:
standing posting authority cannot run it. When the company has entries, the
command refuses with `company_has_entries`. Start a new company folder for books
in another currency.

## The currency from imports

When the company has no entries and `currency_source` is `default` or
`inferred` (not `set`), the first import that names a currency makes that currency
the company currency. Adoption can therefore happen again while an inferred company
still has no entries. It writes `currency` and `currency_source: "inferred"` and
adds the audit event `currency-inferred` before any row is posted. Only the write
step adopts: the importer, in the same ledger transaction as the postings (or just
before the first staged row when nothing posts), a statement file once for the
whole file under the company write lock, and the QuickBooks opening apply, in the
same ledger transaction as the opening entry. A failed write leaves the currency,
the ledger meta and the audit log unchanged. A statement file in another currency
with no row to import (every row outside the boundary, held or already imported)
is refused with `reason: nothing_to_adopt` and writes no statement record. Inspections,
previews, dry runs, `books qb sync-import` and the Stripe plan only check, and
write nothing. "No entries" also means no review queue items, staged rows,
duplicate candidates or statement records. Saved QuickBooks snapshot evidence is
evidence, not an entry, and does not count.
These imports can name a currency:

- OFX, QFX and QBO files: `CURDEF`, or a `CURRENCY`/`CURSYM` on a transaction.
- Bank and card CSV files: a currency code or an unambiguous symbol (`€`, `£`,
  `₹` and others) in a money column, a header such as `Amount (EUR)`, or a
  currency column. A plain `$` is not counted, because many currencies use it.
- Stripe: the currency of the balance transactions (not the charge or invoice
  currency). For a company with entries, a balance transaction in another
  currency is not staged: it stays unresolved with `unsupported_currency`, and
  the other rows are staged.
- QuickBooks API snapshots: the home currency (`CurrencyPrefs.HomeCurrency`).
  `books qb sync-import` reports it as `adopts_currency`; applying the opening
  sets it.
- BankSync and any feed whose normalized rows carry `currency`.

A file with no currency marker (most US bank CSV files) names no currency. It is
read in the company currency.

QuickBooks report exports and accrual opening reports are read in the company
currency: an amount that shows another currency (such as `€1,234.00` in a USD
company, or `$` in a EUR company) is refused with `unsupported_currency`.

An import stops with the error code `unsupported_currency` and writes nothing when:

- it has amounts in more than one currency (`reason: mixed_currency`), or
- its currency differs from the company currency and the company has entries or
  a person set the currency (`reason: currency_mismatch`). The message names
  both currencies.

Ask for a file in the company currency, or a file already converted to it. If the
company has no entries yet and keeps its books in the file's currency, run
`books entity set-currency` before the import. A dry run (`--dry-run`) makes the
same checks and writes nothing.

## Propose a currency before onboarding

`books entity infer-currency --input FILE [--input FILE ...]` reads files and
writes nothing. It prints `currency` (or null), `source` (`inferred`, `mixed`,
`unsupported` or `none`) and `evidence` (the currencies found in each file). A
host can call it before onboarding finishes, so that onboarding does not need to
ask.

## Cloud contract

Adoption is allowed under preparation authority, so onboarding does not need to
ask. A hosted run that adopts a currency changes `entity.json` and the ledger
`meta` together. The host MUST save `entity.json` from that run together with
`ledger.sqlite`, and then read `currency` again. If the host saves the ledger and
not `entity.json`, the hosted migration check refuses with `unsupported_currency`
and `company_settings_out_of_date`, and `currency_settings.message` names both
currencies and says that the saved company settings are out of date. A local load
of the company repairs `entity.json` from the ledger.

## For AI agents

- Read `currency` in `entity.json`. When it is absent, the company currency is USD.
- You may run `books entity infer-currency` and propose `books entity set-currency`
  to the person, with the evidence. Do not apply it yourself. A person applies it.
- Never convert amounts between currencies. Never put amounts in another currency
  into the books.

## Outputs

- Report text and `books ask` answers print money with the company currency, for
  example `€1,234.50` or `CHF 1,234.50`. USD output is the same as before.
- `books accrual capabilities --entity PATH` and `books accrual schema --entity PATH`
  show the company currency in the currency fields. Without `--entity` they show
  USD, as before.
- Tax outputs (`books tax map`, `books tax export`, `README.txt` and
  `manifest.json`) stamp `currency` when the company currency is not USD, and add
  the flag `tax_mapping_is_us`: "The tax mapping
  follows US forms. The company currency is EUR. The preparer decides what
  applies." The engine does not block or refuse a tax output for this reason.
- A migration bundle carries the company currency in `source_entity.currency`.
  `migration_bundle.require_target_currency` refuses a bundle whose currency
  differs from the target company; the error message names both currencies.
