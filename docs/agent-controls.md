# Caller controls for connected agents

A host that runs the engine for a connected AI agent can set environment
variables to apply controls to one command. The variables are not authority by
themselves. The host sets them for the process that runs the command, and it
must not let the agent change them. They only add restrictions. The engine ignores these three variables in a `.env` file, so only the real process environment can set them.

| Variable | Effect |
|---|---|
| `SLASHBOOKS_ACTOR` | An opaque actor string. Each new suggestion records it as `created_by`. At most 200 characters, with no control characters and no leading or trailing whitespace. An invalid value stops the command before any write (`invalid_actor`). |
| `SLASHBOOKS_FORBID_SELF_APPROVAL` | When set to `1`, a command that approves a suggestion refuses if `SLASHBOOKS_ACTOR` equals the suggestion's `created_by`. It also refuses (`invalid_actor`) when `SLASHBOOKS_ACTOR` is not set. |
| `SLASHBOOKS_ENFORCE_ASK_FIRST` | When set to `1`, `queue propose*`, `confirm*` and `correct` refuse an item above the ask-first amount (`above_ask_first_amount`). |

## Settings a person applies

Some commands change a company setting, not a transaction. An agent can run the
preview and show it. A person runs the apply step. Standing posting authority
(see [standing policy](standing-policy.md)) never covers it, and the trusted
preparation runtime refuses it.

| Command | What it sets |
|---|---|
| `books entity set-currency PATH --currency CODE --apply` | The company currency, only while the company has no entries. See [company currency](company-currency.md). |
| `books entity set-view PATH --view cash\|accrual --apply` | The default report view. |
| `books accrual enable apply` | Accrual tracking from a tracking start date. `enable preview` is read-only. |
| `books entity set-fiscal-year PATH --start MM-01 --apply` | The fiscal-year start. |
| `books entity set-tax-method PATH --method cash\|accrual --apply` | The tax method. The tax outputs follow it. |
| `books entity set-tax-profile PATH ... --apply` | The prior return method and the entity type. |
| `books accrual schedule tax-treatment ... --apply` | How a cash-method filer deducts a prepaid schedule. |
| `books accrual schedule\|item deposit-kind ... --apply` | Whether a deposit is an advance payment or refundable. |

Without `--apply` these commands preview and write nothing. `books entity
infer-currency` only reads files, so an agent can run it. Closing and reopening a
period also need a person. See [the tax handoff](tax-handoff.md) for the tax
settings and [accrual books](accrual.md) for accrual tracking and the default view.

## Reporting goals and report categories

These commands support the owner's reporting goals. See
[reporting goals](statements.md#reporting-goals-and-report-categories).

| Command | Changes the books? | What it changes |
|---|---|---|
| `books entity set-reporting-goals PATH ... --apply` | No | `reporting_goals` in `entity.json` and one audit event. No entry, account or report changes. |
| `books entity account-add PATH --account A --category` | No | Adds one empty income or expense category to the account catalog. No entry or amount changes. |
| `books ledger reclassify ... --apply` | Yes | Writes a reversal and a replacement for each moved entry. |

Without `--apply`, `set-reporting-goals` and `ledger reclassify` preview and write
nothing. A host can let its automation record goals that the owner confirmed,
because the goals change no amounts. Standing posting authority does not cover
`ledger reclassify`, and the trusted preparation runtime refuses it. A person
approves each move. The command refuses an entry in a locked month
(`period_locked`).

## Remove an unused category

`books entity account-remove PATH --account ACCOUNT [--added-event ID]` removes an
income or expense account from the account catalog. It does not change the books:
no entry changes, and the account has no postings. A host can use it to undo a
category that it added, while nothing uses the category.

The command is narrow. It removes an account only when `books entity account-add`
created it: the audit log has the event that `account-add` writes when it creates
an account (an `intent` event of session `entity-account-add` with the description
`Add account <account>: open <date>, currency <code>`). `--added-event ID` names
that event. Without it, the command uses the latest such event for the account.

- Without `--apply` the command shows a preview and writes nothing.
  `--apply --plan-hash H` removes the account and writes the audit event
  `account-removed` with the account, its open date, its currency, the id of the
  account-add event and the plan hash.
- The plan hash covers the audit chain head and a fingerprint of every settings
  file that the checks read. A change after the preview, for example a removal
  and a new `account-add` of the same account, or a new `ask_first_above` in
  `trust-policy.json`, gives `stale_preview`.
- When the account is not in the catalog, the command gives `already_applied` and
  writes nothing. This also applies to an exact retry with the same plan hash.
- The command takes the company write lock without the `entity.json` repair, so a
  refused or no-op apply writes no company file.

The command refuses before any write:

| Code | Reason |
|---|---|
| `invalid_account` | The name is not a valid account name, for example it has a control character such as a final newline. |
| `protected_account` | The account is not an income or expense account, or the engine creates or names it: the starter catalogs, the accrual starters, the Stripe accounts and the report accounts (one list, `engine_accounts.engine_owned_accounts()`). |
| `not_added_by_command` | No account-add event shows that `account-add` created the account. |
| `added_event_mismatch` | `--added-event` does not name the latest account-add event of the account. |
| `account_in_use` | A posting in any period (reversals too) or a balance assertion uses the account. |
| `account_referenced` | Company settings or records name the account: `entity.json` (bank mappings, related entities, tax lines and other settings), `trust-policy.json`, learned context (payee rules, accrual rules), an open review item, a staged row, an accrual record, a saved source row (`source_transactions`) or an import session (`import_sessions`). `refs` names the file or the table and record id. The check parses the JSON and compares decoded names, so a name written with JSON escapes is found too. A saved accrual rule that selects the account through a parent account also counts, when the parent would then select no account. `refs` lists up to 20 places. |
| `reference_check_failed` | A settings file or folder is a symbolic link, cannot be read, is not valid JSON, or does not have the expected shape (for example a `trust-policy.json` that is not an object, or an accrual rule whose `match` is not an object), so the references cannot be checked. |
| `audit_history_missing` | The ledger has entries and no audit history. |

The reference check does not read these, on purpose:

- the audit log, which keeps historical provenance (earlier `account-add` and
  `account-removed` events, for example);
- the narration and metadata of entries and postings (the postings are checked by
  account);
- files outside the settings folders: `reports/`, `ingestion/` and
  `business-profile.md`.

`books queue` teaching checks the account catalog under the same company lock, so a
removal cannot come between that check and the save. `books ledger reverse`,
`correct`, `reclassify-bank` and `split-equity` also hold that lock from the entry
lookup and the catalog read to the commit, so they decide under the lock whether a
missing account gets an open directive. The reference check walks JSON with its
own stack; JSON that the parser cannot read at any depth gives
`reference_check_failed`.

Standing posting authority does not cover the command, and the trusted
preparation runtime refuses it.

## Record that onboarding is complete

`books entity onboarding-complete PATH --via agent|meeting|staff [--on YYYY-MM-DD]`
records that the company finished onboarding. It writes `onboarding_completed`
(`on`, `via`) to `entity.json` only. It changes no ledger data, no other setting
and no queue item. A valid first record is kept: a second run prints
`already_complete` and writes nothing. `--on` defaults to the machine date
(`date.today()`), because the engine keeps no company time zone. A date after
today (the later of the machine date and the UTC date) is refused.

With local books, the books-onboard skill runs it with `--via agent` when
onboarding is done. A host can limit who records it. On an online server,
only the setup meeting, staff and a person's own connected agent can record
completion. The server's own automation cannot, so an automated run cannot
mark its own company as set up. Outside an online server, `--via agent` works as written.

## Who made a suggestion

`books queue propose`, `propose-group`, `propose-split`, `propose-transfer`,
`propose-intercompany` and `propose-related` read `SLASHBOOKS_ACTOR`. When it is
set, the queue item stores it in `created_by`. The field is absent when no actor
was set. A later proposal for the same item replaces the creator with the actor
of that proposal, because that actor wrote the content that is now waiting.

`books queue show` prints `created_by` in the item. `books queue list --json`
prints every item as `{"schema_version": 1, "kind": "queue_items", "count": N,
"items": [...]}`. The text output of `queue list` does not change.

## Refuse self-approval

With `SLASHBOOKS_FORBID_SELF_APPROVAL=1` and `SLASHBOOKS_ACTOR` set,
`queue confirm`, `confirm-group`, `confirm-split`, `confirm-transfer` and
`queue correct` refuse when the actor equals the item's `created_by`. The
refusal happens before any write. The command exits with code 1 and prints this
JSON on standard error:

```json
{"error": {"code": "self_approval", "message": "..."}}
```

An item with no `created_by` is not affected when an actor is set. With the control on and no actor set, the command refuses with `invalid_actor` (fail closed).
`confirm-group` checks every item first, so a refusal leaves no partial group.

## Ask first above an amount

The ask-first amount is a limit above which a person decides. It applies to the
absolute amount, so a payment of -900.00 and a receipt of 900.00 are the same.
An amount equal to the limit is not above it.

Set the limit in one of two places:

- `ask_first_above` in the company's `trust-policy.json`, for example
  `{"ask_first_above": "500.00"}`. A `null` value means no limit.
- `ask_first_above` in a version-1 standing policy
  (`StandingPolicy`, see [standing policy](standing-policy.md)). It applies to
  every command that runs inside the trusted preparation boundary.

When both are set, the lower amount applies. The setting only adds holds. A
setting that cannot be read (for example `"lots"` or a negative number) holds
every non-zero amount.

**Import.** `queue.eligible_for_autopost` returns false for a row above the
limit, so a learned category or a saved rule does not post it. The row goes to
Review, like a row that waits for accrual treatment. The staged row gets
`review_hold_reason: "above_ask_first_amount"`. A row without a readable amount
waits as well. A row above the limit with no matching rule is reported as `above_ask_first_amount`, not `needs_category`. A routine plan reports a rule match above the limit as
unresolved with the reason `above_ask_first_amount`.

**Stripe.** `stripe_plan` lists a row above the limit as unresolved with the reason `above_ask_first_amount`. `stripe_apply` does not record it.

**Propose and approve.** Inside the trusted preparation boundary the limit is
always enforced, and the lower of the company and standing-policy limits applies. Outside it, the limit is enforced when
`SLASHBOOKS_ENFORCE_ASK_FIRST=1`. Then `queue propose`, `propose-group`,
`propose-split`, `propose-transfer`, `confirm`, `confirm-group`,
`confirm-split`, `confirm-transfer`, `confirm-transfer-match` and `correct`
refuse an item above the limit before any write. The command exits with code 1 and prints
`{"error": {"code": "above_ask_first_amount", "message": "..."}}` on standard
error. A split compares the amount of its source transaction. A transfer pair
compares the absolute amount of the pair. A transfer match compares the
absolute amount of the staged row. `confirm-group` and `propose-group`
check every item first.

**Routes that do not check the limit.** Routine captured decisions (a person
decided), `backtest` category rules, corrections of posted entries
(`ledger correct`, `reclassify-bank`, `reclassify`, `split-equity`) and the `accrual` commands
do not apply the limit. A payout bank deposit matched by `stripe_plan` is also not checked.

## No posting before an account's open date

Each account in the catalog has an open date: the first day the books record
activity for it. The books often start an account with an opening balance on
that day, and that balance may already include earlier activity. So the books
do not record a posting before the open date. The open date alone does not
show that a given row is already in an opening balance.

Every ledger write refuses such a posting, with the codes that a routine plan
uses:

- `source_catalog_conflict`: the row's own bank or card account opens after
  the row date.
- `category_catalog_conflict`: another account of the entry opens after the
  entry date.

This applies to `queue confirm`, `confirm-group`, `confirm-split`,
`confirm-transfer` and `correct`, and to `ledger correct`, `reclassify-bank`,
`reclassify` and `split-equity`. A refused command writes nothing. The whole
entry is refused, also when only one posting is early. The row stays in review
and its suggestion stays open. `queue` commands exit with code 1 and print
`{"error": {"code": "source_catalog_conflict", "message": "...", "action": "..."}}`
on standard error. Inside the trusted preparation boundary the refusal is a
review hold (`PreparationDenied`) with the same code.

`confirm-group` checks every item before the first write, and it holds the
company write lock from that check to the last write. If an item is early, the
command writes nothing. The writes are separate ledger transactions. If the
ledger write refuses an item after the check (for example because a change
outside the company lock moved an open date), that item stays in review and the
items before it stay recorded.

At import, a row that a learned category would post goes to Review instead,
with `review_hold_reason` set to the code.

A ledger write cannot move the open date of an account in the catalog to an
earlier day (`open_date_change_not_authorized`). Three commands set open dates,
with their own permission rules:

- `books entity account-add --entity PATH --account ACCOUNT --open-date YYYY-MM-DD`
  moves an open date earlier. The trusted preparation boundary does not support
  this command, so standing automation cannot run it.
- `books accrual opening apply` sets the open dates of its chart from its
  preview.
- `books entity set-books-from --apply` moves fallback-dated generic accounts
  to a books scope that a person previewed (see below).

Before you move an open date, check whether an opening balance already includes
the earlier activity.

### The books scope (`books_from`)

A fiscal-year boundary is not a posting start. A business can start its books
for 2026 and still need its 2025 entries.

`books_from` is the host-stated start of the historical scope, and the source
of the default open date of the generic accounts. It is not a posting fence:
no ledger write reads it. The open date of each account stays the only
open-date fence, and in enforced mode the books start policy below is the
host's fence too.

The generic accounts are the income and expense accounts, and the two ordinary
owner equity accounts of the starter chart, `Equity:Owner-Contributions` and
`Equity:Owner-Draws`. The books scope never opens another account:

- Bank, card and other balance sheet accounts. Each needs its own start (an
  explicit `--open-date`, a cutover or an opening).
- Control equity accounts: `Equity:Opening-Balances`,
  `Equity:Opening-Balance-Adjustments`, `Equity:Retained-Earnings`,
  `Equity:Prior-Periods-Net-Income`, `Equity:Accrual-To-Cash-Adjustment`, and
  every equity account that a person adds (its role is not known). An opening
  operation sets the date of an opening equity account.

The commands:

- `books entity init PATH --books-from YYYY-MM-DD` opens the starter generic
  accounts on that day. The other starter accounts keep the fallback date,
  2026-01-01. With `--cutover-date`, every starter account opens on the
  cutover, as before, and the cutover is the books scope. A `--books-from`
  that differs from `--cutover-date` is refused.
- Without `--books-from` and `--cutover-date`, every starter account opens on
  the fallback date 2026-01-01, as before.
- `books entity account-add` without `--open-date` opens a new generic account
  on the books scope (without a recorded scope, on the `entity.json` cutover),
  and another new account on the fallback date. An existing account keeps its
  open date.

The ledger meta key `open_date_provenance` records the books scope and the
source of each account's open date: `default` (the fallback), `scope`,
`cutover`, `user` (`--open-date`), `import`, `opening` or `legacy` (no
reliable source). The audit seal covers it. Every catalog writer keeps it in
the same transaction as its change. A ledger made by an earlier version gets
the record at its first catalog write with a source; the accounts already in
it are `legacy`. The record holds a fingerprint of the catalog rows. If an
earlier engine version (after a rollback) or a direct edit changes the
catalog, the fingerprint differs, and no marker is evidence until a person
reviews each account (`provenance: "stale"` in the preview). An earlier
version's command that writes nothing leaves no trace, so a ledger without
reliable markers never gets automatic evidence.

`books entity set-books-from PATH --books-from YYYY-MM-DD` previews a new books
scope and writes nothing. It lists each account that moves, with its
`evidence`, and each account that stays, with its `reason` and, for an
uncertain account, what is known (`in_starter_chart`, and the account-add and
account-remove audit events). `--apply --plan-hash H` runs only the previewed
plan and writes the audit event `books-from-set`. No entry changes. A replay of
an applied plan returns `already_applied`. Standing automation cannot run this
command.

A generic account moves only when no bank or CSV mapping names it and one of
these is true:

- its open date has the marker `default` or `scope`, and the record is not
  stale (`recorded_default`, `recorded_scope`);
- it has no reliable marker, it opens on 2026-01-01, and a person names it with
  `--account` after review (`named_by_person`). The preview lists these
  accounts in `effects.uncertain`. A starter name and the date 2026-01-01 are
  not evidence.

An account with `user`, `import`, `opening` or `cutover` provenance never
moves. The preview refuses a scope before the company cutover
(`scope_before_cutover`), before a QuickBooks or accrual opening
(`scope_before_opening`, because such an opening summarizes the whole company
before its day), later than the current scope (`scope_not_earlier`), or over a
closed period (`closed_period`).

A statement opening is for one account, so the preview lists it in `openings`
and does not refuse. The account-specific opening controls fence that
account's earlier history, not the catalog date alone: `books statement
record-opening` refuses while earlier rows of the account wait
(`rows_waiting_before`), and intake keeps a row dated before the account's
opening entry out of review (`before_opening`).

The exact reversal of an entry in the books is allowed, so a person can remove
an early posting that an older version recorded. An exact reversal has the
date of the original entry and the same postings with each amount negated, and
the original is not reversed yet. Any other entry with `reverses` metadata is
checked like every other entry.

## Books start policy (set by a host)

The open date of an account and an opening balance do not say from which day
live sources may add rows. A host that keeps an authorized books start for each
account can pass it to any command that writes the ledger. Set the environment
variable `SLASHBOOKS_BOOKS_POLICY` to the path of a JSON document:

```json
{"schema_version": 1, "mode": "enforced", "epoch": 4,
 "accounts": {
   "Assets:Bank:Checking": {"state": "authorized", "start_day": "2026-03-15", "version": 2},
   "Liabilities:Card-1234": {"state": "authorized_pending_opening", "start_day": "2026-03-15", "version": 1},
   "Assets:Bank:Savings": {"state": "unset"}}}
```

Without the variable, nothing changes. If the variable is set, the document must
load. A missing document refuses every ledger write with
`BOOKS_POLICY_REQUIRED`, and a bad document with `BOOKS_POLICY_INVALID`. The
engine never falls back to the earlier behavior. `"mode": "legacy"` turns the
check off.

In `enforced` mode the ledger write checks each posting line of each new entry
on an account in scope. The scope is every account that the policy names, every
ledger account of a bank or CSV mapping, and the account of each source row in
the write. The engine never uses an account name to decide the scope. A
transfer between two in-scope accounts is checked on both sides.

| State | Live postings to the account | Code |
|---|---|---|
| no entry in the policy | refused | `BOOKS_POLICY_REQUIRED` |
| `unset` | refused | `BOOKS_START_UNSET` |
| `authorized_pending_opening` | refused, also on and after `start_day` | `OPENING_REQUIRED` |
| `authorized` | refused before `start_day`, allowed on and after it | `BOOKS_DATE_BEFORE_AUTHORIZED_START` |

A refusal has the same effect as an open-date refusal: nothing is written, the
row stays in review, `queue` commands exit with code 1, and standing automation
gets a review hold with the code. `queue` commands print
`{"error": {"code", "message", "action", "account", "state", "start_day",
"entry_date", "source_id", "policy_epoch", "account_version"}}` on standard
error. A routine plan gives the code as the reason, and import holds a row that
a learned category would post, with the code as its `review_hold_reason`.

The exact reversal of an entry in the books is not refused (the same rule as
above). Entry metadata such as `import-source`, a source ID or `reverses` never
makes an entry exempt.

This check inside a command gives early feedback. It is not the fence, because
the session that runs the command controls the variable and the file. The
opening operations (`books qb sync-opening --apply`, `books qb import-opening`,
`books statement record-opening`, `books accrual opening apply`) also write
outside it or under their own rules.

### The trusted check before publication

The host runs the fence in a process that the session cannot configure, after
the command and before it publishes the result:

```sh
python -I -m bookkeeping.ledger.books_policy_check check --request REQUEST.json \
    --base PUBLISHED_ENTITY_DIR --candidate PROPOSED_ENTITY_DIR
```

`REQUEST.json` comes from the host's own copy of the policy for the job:
`{"schema_version": 2, "policy": {...}, "scope": [ledger accounts],
"opening_authorizations": [...]}`. The check reads no environment variable. It
refuses a proposed ledger that drops or edits an entry that is already published
(`BOOKS_LEDGER_HISTORY_CHANGED`). An entry is compared on every entry and
posting field, posting metadata included. The order of entries does not
matter. It checks every entry that the proposed ledger adds with the rules
above. Scope includes the request scope and the accounts named by the policy.
The CLI and `check_workspaces` also read both workspaces. They add two sets of
exact ledger keys from each workspace:

- Accounts explicitly classified as bank, credit card or cash in the catalog.
  An unmapped account can be in scope. The checker uses the stored kind and
  does not infer it from the account name.
- Ledger targets from the bank and CSV mappings. Many provider accounts can map
  to one ledger account.

Taking the union of the published and proposed sets keeps a published account
in scope for the check even if the proposal changes its kind or mapping.
The entry-only Python function
`check_ledger_delta(request, base_entries, candidate_entries, mapped_accounts)`
cannot discover these sets. Direct callers must read both catalogs and both
workspaces’ mappings and supply that union in `mapped_accounts` or in the
host-controlled request scope.

Scope determines which posting accounts the checker examines. The host still
decides who can change a classification, how to handle postings to unclassified
accounts, and whether to authorize a start. It also binds the checked
workspaces to publication.

An added entry that reverses a published entry is not refused when it is the
exact reversal of the only published entry with that source ID, no published
entry reverses that entry already, and no other added entry claims it. A
reversal of an entry that is not published yet is checked like any entry.

An opening on an account in `authorized_pending_opening` passes only when the
host gives an opening authorization that matches the entry. In request version
2 an authorization has exactly `account`, `start_day`, `version`, `operation`,
`operation_id`, `entry_source_id` and `entry_fingerprint`. It matches an added
entry with the `start_day` and `version` of the policy entry, the entry date,
the entry source ID and the entry fingerprint. The fingerprint
(`books_policy_check.entry_fingerprint`) is the SHA-256 of every entry field and
every posting field: date, narration, payee, flag, tags, links, entry metadata,
and each posting's account, amount, currency and metadata. A version 2 request
without a fingerprint, with a fingerprint that is not 64 lowercase hexadecimal
characters, or with the version 1 `postings` field is invalid. Version 2 does
not fall back to version 1. A version 2 request that contains a non-finite
number is invalid. Two authorizations for one account, start day and version
make the request invalid, and an authorization that matches two entries matches
none. Every other opening is checked like any entry, so an opening before the
start or on an `unset` account is refused.

Request version 1 is the released version, and the check still reads it with
its released meaning. It is weaker. Its authorization has `postings`
(`[account, "amount", currency]`, with the posting metadata as a fourth item
when the posting has any) in place of `entry_fingerprint`. It binds the entry
date, the source ID and the postings only, so an entry with another narration,
payee, flag, tags, links or entry metadata still matches. Use version 2.

Request version 3 is for one operation only, `accrual_opening_prior_day`: a
fresh accrual opening (`books accrual opening preview` on a company with no
accrual tracking start). The native accrual opening posts on the day before the
cutover, so a version 1 or version 2 authorization, which matches the entry date
with `start_day`, never matches it. A version 3 authorization has the version 2
fields. `start_day` is the reviewed cutover, and the check derives the entry date
as the day before it. A request cannot give another date. The request has one
authorization for each protected account that the entry posts to:

- Each authorization has its own `operation_id`. Two authorizations with one
  `operation_id` make the request invalid.
- All authorizations have the same `start_day`, the same `entry_source_id` (the
  `accounting_plan.id` of the reviewed preview, which is the entry's source ID)
  and the same `entry_fingerprint` (`proposed_entry_fingerprint`).
- The request is invalid when it has no authorization, when its policy is not
  `enforced`, when a `start_day` is not exact `YYYY-MM-DD` text, or when it names
  any other operation. Versions 1 and 2 do not read `accrual_opening_prior_day`.

The authorizations pass as one group, or none of them passes. The group passes
only when the published books have no entry, the proposal removes no entry and
adds exactly one, and all of these are true:

- The entry has the authorized source ID and fingerprint, and its date is the
  day before `start_day`.
- Its metadata is exactly `source-id`, `import-source` = `accrual-opening`,
  `accrual-opening` = `true`, `coverage-id` = `opening` and `cutover-date` =
  `start_day`. A tracking opening or another opening kind does not pass.
- The authorized accounts are exactly the protected accounts (the scope above)
  that the entry posts to, and the entry has no zero line on them.
- For each account, the policy entry is `authorized_pending_opening` with the
  authorized `start_day` and `version`.

When one of these is false, the check refuses with
`BOOKS_OPENING_AUTHORIZATION_MISMATCH` and the group lets nothing through. The
check does not prove the support records, the account open dates or the replay of
the native plan. The host applies the reviewed native plan and compares it.

A row that a person left out of the books (`outside_books`, in
`source_disposition`) while its account was `authorized_pending_opening` keeps
that decision when the opening is recorded. A statement or QuickBooks opening
completes the decision when it is dated on `start_day`, as before. A fresh
accrual opening (`import-source` = `accrual-opening`) is the `accrual` opening
kind. It completes the decision only when `evaluate` gets
`opening_authorization`: the exact version 3 request above that published it.
All of these must be true:

- The entry is a fresh accrual opening for `start_day`, dated the day before it.
- The request is a valid version 3 request. Its `entry_source_id` and
  `entry_fingerprint` are those of the entry, its `start_day` is the start of the
  decision, and each `version` is the pending version in its policy.
- The authorized accounts are exactly the Assets and Liabilities accounts that
  the entry posts to, receivables and payables included. Equity is left out.
- Each of those accounts is `authorized` in the current policy with the same
  `start_day` and the next version. Put every opening balance account in the
  books start policy, so that the version 3 check also needs its authorization.
- The `epoch` of the policy in the request is at or after the epoch that the
  decision captured, and the current epoch is exactly that epoch plus one (the
  policy that the publication made). An older request is refused.

The engine does not authenticate the `operation_id` values. It checks only that
each one is non-empty text and unique, and it records them. A trusted host must
supply the immutable origin of the reviewed preview and its evidence, and bind
the request to it.

The first evaluation gives `transition_pending` with
`expected_accrual_opening_recorded` evidence. The evidence records the binding of
the request (with its policy epoch) and the new open date of the account (the
native opening opens the account on the entry date). The host records it one
time. Recorded evidence is not issued again, so it stays valid at any later
epoch. Each later evaluation checks the evidence again: an integer
`schema_version`, a positive safe integer version (at most 2^53 - 1) on every
account, and every recorded account still `authorized` at the same start with
the next version. Otherwise the decision is invalidated. When a rule is false,
the decision is `invalidated` with one or more of these triggers
(`ACCRUAL_COMPLETION_REASONS`):

| Trigger | Meaning |
| --- | --- |
| `ACCRUAL_AUTHORIZATION_REQUIRED` | No request was given. |
| `ACCRUAL_AUTHORIZATION_INVALID` | The request is not a valid books start check request. |
| `ACCRUAL_AUTHORIZATION_NOT_PRIOR_DAY` | The request is version 1 or 2. A same-day authorization never completes a prior-day opening. |
| `ACCRUAL_AUTHORIZATION_STALE` | The authorized start or version is not the start or the pending version, or a version is not a safe integer. |
| `ACCRUAL_AUTHORIZATION_EPOCH` | The policy epoch of the request is before the captured epoch, or the current epoch is not that epoch plus one. |
| `ACCRUAL_OPENING_NOT_FRESH` | The entry metadata is not exactly that of a fresh accrual opening for this start, or it posts outside Assets, Liabilities and Equity. |
| `ACCRUAL_OPENING_NOT_PRIOR_DAY` | The entry is not dated the day before `start_day`. |
| `ACCRUAL_ENTRY_MISMATCH` | The source ID or the fingerprint is not that of the entry. |
| `ACCRUAL_ACCOUNTS_MISMATCH` | An Assets or Liabilities account of the entry is not authorized, or an authorized account is not in the entry. |
| `ACCRUAL_ACCOUNT_NOT_GOVERNED` | An Assets or Liabilities account of the entry is not `authorized` in the current policy with the start and the next version. |

A tracking opening (`accrual-tracking-opening`) is never an opening here. It adds
accrual balances to books that already started, and it is dated on the tracking
start, so it stays ordinary booked activity. A context document that names it as
an opening is refused. The completion changes no balance, never relabels an
accrual opening as a statement opening, and never allows a booking.

The source ID of a fresh accrual opening is the `operation_id` of its request,
and the ledger records it. If that ID or one of its aliases (`banksync:abc` and
`abc`) names a staged row, the row counts as booked: a preview refuses it
(`already_booked`), and a stored decision is invalidated
(`DISPOSED_ROW_BOOKED`). A decision previewed when the accrual opening is
already live keeps that opening in its basis and resolves unchanged.

The host makes an authorization only from its own receipt of a reviewed opening
operation and the preview that the person reviewed. Each supported opening
preview prints `proposed_entry`, the complete entry that the apply records (every
model field, with its source ID and metadata), and `proposed_entry_fingerprint`.
Read `proposed_entry` with the strict reader
`source_disposition.entry_from_document`: it refuses a missing or an unknown
field, so a later change to the entry model stops the host until it reads the new
field. Make sure that `entry_fingerprint` of the entry it reads is
`proposed_entry_fingerprint`. A preview adds no ledger entry.

| Opening operation | Preview with `proposed_entry` | Other state that the preview changes |
| --- | --- | --- |
| `quickbooks_sync_opening` | `books qb sync-opening` without `--apply` (`opening`), also with a chart choice (`preserve` or `rebuild`) | It saves the snapshot reference data (`sync-import`, in `sources/quickbooks/<snapshot ID>/`), as before, and the empty `.books.lock`. No ledger change. |
| `quickbooks_import_opening` | `books qb import-opening --dry-run` | None |
| `statement_opening` | `books statement record-opening --statement-id ID --dry-run` | The empty entity lock file `.books.lock` |
| `starting_balance` | `books statement record-opening --account A --on DAY --amount N --dry-run` | The empty entity lock file `.books.lock` |
| `accrual_opening` | `books accrual opening preview` and `books accrual migration preview` (a plan that is `ready` and not a replay) | None |
| `accrual_opening_prior_day` (version 3 only) | `books accrual opening preview` on a company with no accrual tracking start (a plan that is `ready` and not a replay) | None |

These paths have no supported preview of the complete entry, so they are
BLOCKED for a version 2 authorization: `books qb import-opening` on an accrual
company (its dry run refuses; use `books accrual migration preview`), and
`books qb repair-opening --apply` (its inspection names the invalid entries,
not the replacement entry). The command exits with 0 (`ok`), 1 (`refused`:
publish nothing) or 2 (a bad request or workspace).

The result has `request_sha256`, `base_sha256`, `candidate_sha256` and
`package_version`. The package version is a label, not a build identity.
`build_receipt(result, engine_build=..., ...)` formats a receipt and refuses one
without a full 40-character engine commit. It is a formatting helper, not
attestation: any Python code can call it with any values. The trusted origin of
`engine_build` and of every other receipt field, and a trusted channel for the
receipt, are host requirements only.

## The opening balance of each account

After a person records an opening, a host can show the opening balance of each
account. The host does no accounting math. It calls
`bookkeeping.opening_presentation.opening_presentation(entry, accounts,
financial_kinds)`. The function reads no file and writes nothing.

The entry document is presentation input only. It is not proof that the entry
is committed or current in the ledger. A caller that needs that proof must check
it separately, for example with a trusted verification of the live ledger
entry.

- `entry` is the opening entry: an `Entry`, or the entry document that a
  preview prints as `proposed_entry` (or the `entry` of a recorded opening).
  The entry must come from a statement, QuickBooks or accrual opening, and it
  must not be a reversal.
- `accounts` is the list of the Assets and Liabilities accounts that the entry
  posts to. The names are unique and sorted by their UTF-8 bytes. Equity
  accounts are not in the list.
- `financial_kinds` maps an account to its explicit financial kind, as
  `LedgerStore.load_financial_kinds` returns it. An account without a key is
  unclassified.

The result has one row for each name in `accounts`, in the same order:

```json
{"account": "Liabilities:CreditCard:Visa", "status": "presented",
 "financial_kind": "credit_card", "amount": "2000.25", "currency": "USD"}
{"account": "Liabilities:Other:Loan", "status": "refused",
 "reason": "financial_kind_missing", "financial_kind": null, "message": "..."}
```

`amount` is in the presentation sign: money held for an asset account, money
owed for a liability account. It has exactly two decimals, no thousands
separator, and a leading `-` when it is negative. `currency` is the currency of
the account's lines in the entry. For a statement opening, `amount` is the
`opening-balance` metadata of the entry.

The supported financial kinds are `bank` and `cash` on Assets, `credit_card` on
Liabilities, and `other` on Assets or Liabilities (for example a receivable, a
payable or a loan). A row is `refused` with one of these reasons:
`financial_kind_missing`, `financial_kind_unsupported`,
`financial_kind_mismatch`, `mixed_currency` or `opening_balance_differs`. Show
a refused row as an account that needs a review. Do not compute an amount for
it.

If the list is not exactly the Assets and Liabilities accounts of the entry,
the function raises `OpeningPresentationError` with the code
`accounts_mismatch`, and `detail` names the accounts in `not_posted` and
`not_listed`. The other codes are `invalid_entry`, `not_an_opening`,
`invalid_accounts` and `invalid_financial_kinds`.

The same check is available as a command:
`python -I -m bookkeeping.opening_presentation show --request R.json --entity DIR`.
`R.json` is `{"schema_version": 1, "entry": {...}, "accounts": [...]}`. The
command reads the financial kinds from the ledger of `DIR` read-only. It exits
with 0 when every row is presented, 1 when a row is refused, and 2 for a bad
request.

## Read the amount of an item

`bookkeeping.queue.amount_for_item(entity, item_id)` returns the absolute
`Decimal` amount that an item decides, or `None` when the amount cannot be
read. Treat `None` as above the limit. It raises `FileNotFoundError` when the
item does not exist. `queue.ask_first_above(entity)` returns the effective
limit or `None`. `queue.amount_is_above_ask_first(entity, amount)` and
`queue.txn_is_above_ask_first(entity, txn)` apply the rule. A host does not need
the private queue functions.

## Amounts in command results

A host reads the result of a command from the work events (`--work-out`, see
[work events](work-events.md)). Each `categorized`, `recorded`, `corrected` and
`reconciled` event has an optional `amounts` list with the amount and accounts
that the engine recorded, as Decimal strings. A row for an entry has an `amount` only when the entry records one bank or card row (two postings, or a reviewed split). An accrual journal or another multi-line entry keeps its `accounts` and has no `amount`. A host can write "matched a
transfer of 500.00 between A and B" without math. The text output of the
commands does not change.
