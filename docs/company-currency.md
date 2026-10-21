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

The ledger also records a non-USD company currency in its `meta` table, so that
new accounts open in that currency. Every account in the account catalog uses the
company currency.

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
Repeating the same value returns `already_applied`. The command is person-only:
standing posting authority cannot run it. When the company has entries, the
command refuses with `company_has_entries`. Start a new company folder for books
in another currency.

## The currency from imports

When the company has no entries and `currency_source` is `default`, the first
import that names a currency makes that currency the company currency. It writes
`currency` and `currency_source: "inferred"` to `entity.json` and adds the audit
event `currency-inferred` before any row is posted. These imports can name a
currency:

- OFX, QFX and QBO files: `CURDEF`, or a `CURRENCY`/`CURSYM` on a transaction.
- Bank and card CSV files: a currency code or an unambiguous symbol (`€`, `£`,
  `₹` and others) in a money column, a header such as `Amount (EUR)`, or a
  currency column. A plain `$` is not counted, because many currencies use it.
- Stripe: the currency of the balance transactions.
- QuickBooks API snapshots: the home currency (`CurrencyPrefs.HomeCurrency`).
- BankSync and any feed whose normalized rows carry `currency`.

A file with no currency marker (most US bank CSV files) names no currency. It is
read in the company currency.

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
  `manifest.json`) stamp `currency` when `entity.json` records one. When the
  currency is not USD, they add the flag `tax_mapping_is_us`: "The tax mapping
  follows US forms. The company currency is EUR. The preparer decides what
  applies." The engine does not block or refuse a tax output for this reason.
- A migration bundle carries the company currency in `source_entity.currency`.
  `migration_bundle.require_target_currency` refuses a bundle whose currency
  differs from the target company.
