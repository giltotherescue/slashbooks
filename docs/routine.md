# Deterministic routine operator

For accrual companies, ordinary bank intake remains held for explicit accounting
treatment; learned cash categories and routine rules cannot decide recognition or
settlement. An explicitly delegated agent can use the generic accrual preview/apply
contracts under trusted `adjust` authority. Preserve missing-fact holds and exact
evidence. Scheduling a routine never grants close/reopen or publication authority.
See [accrual intake](accrual-intake.md).

The Python routine planner separates intake from approval. It is an internal
building block for a trusted hosted operator, not a policy supplied by an AI
agent. It does not schedule jobs, authenticate people, or write the books.

Set `SLASHBOOKS_INTAKE_ONLY=1` in the trusted compute runner to force every
`import_transactions` caller to stage posted transactions without invoking its
categorizer. Pending transactions remain pending. Direct Python callers can
also pass `intake_only=True`; the CLI exposes `books ingest --intake-only`.
Queued and posted repeats retain normal source-ID deduplication.

## Trusted policy and captured answers

`bookkeeping.routine.routine_plan(entity, policy, source_ids=None,
decisions=None, *, closed_through=None, limit=50, after_source_id='')` reads
existing `staging/pending-categorization.json` and the canonical account catalog.
Both connector intake and the hosted browser document bridge use that worklist;
a review proposal file is not required. The call does not create, quarantine,
or change company files.

The host supplies authenticated policy separately from editable company files:

```json
{
  "version": 1,
  "enabled": true,
  "rules": [{
    "id": "approved-rule",
    "source_account_id": "checking-id",
    "approved_ledger_account": "Assets:Bank:Checking",
    "counterparty_key": "SERVICE",
    "category": "Expenses:Software",
    "max_abs_amount": "50.00"
  }],
  "review_categories": ["Expenses:Software", "Expenses:Fees"],
  "answer_roles": ["admin", "accountant", "client"]
}
```

Rules require exact source-account and normalized counterparty matches. The
optional nonnegative `max_abs_amount` limits the magnitude of the signed amount.
Learned counterparty counts do not grant policy authority. Conflicting matching
rules are held for review. A rule's frozen `approved_ledger_account` must still
match the current source mapping.

A one-time decision contains `id`, `source_ids`, `source_digest`, `source_digests`,
`category`, and `approved_ledger_account`. `source_digests` maps every captured ID
to `routine_source_digest([original_transaction])`. The host freezes these from its saved question;
the answer body selects only a permitted category. The host must authenticate
the answer role against `answer_roles`. A decision cannot mix source accounts,
counterparties, currencies, or amount signs. Overlapping decisions are rejected.
`routine_source_digest(transactions)` hashes canonical, source-ID-sorted
identities: ID, date, signed cent amount, account ID, currency (default USD), and
the same counterparty key used by the normal queue. The source list and digest
must still match the staged records. Once earlier batches have posted, the
remaining records must match their frozen singleton digests; missing members
must already be posted. Thus an interrupted group can resume without extending
its approval to new or changed sources. Mapping authority is bound separately so a
mapping edit cannot change the financial effect of an old answer.

An explicit decision with `category: null` is a human hold for those exact
captured sources. It returns `human_hold`, overrides matching rules, and does
not ask the same category question again. It does not prevent other eligible
sources from progressing.

The planner holds invalid identities/dates/amounts, pending sources, unknown or
conflicting bank/card roles, missing mappings, changed approved mappings,
unsupported currencies, zero amounts, existing open review proposals, and periods
at or before the trusted `closed_through`. It skips posted and reversed sources.
Source and category accounts must exist in the catalog, be open by the source
date, and permit USD. The current ordinary queue writer supports USD; other
currencies remain held. Source bank/card accounts and Uncategorized cannot be
offered as routine categories.

## Apply and resume

The result contains `actions` (`source_id`, `category`, `authority_id`),
`unresolved` source details with stable reason codes, `skipped` IDs/reasons, and
`next_cursor`. It selects IDs lexically after `after_source_id`; action and
unresolved counts together never exceed `limit` (1–500). Removing earlier
confirmed sources does not move the cursor. The host must ensure a later job
revisits newly arrived IDs before an existing cursor.

Apply each action with ordinary `queue.propose` and
`queue.confirm(..., learn=False)` inside one isolated candidate workspace. Save
the authority ID in proposal reasoning/context. The host must revalidate the
current company revision, trusted close boundary, policy, source digest and
mapping before atomic publication. Recover an uncertain publication through
its existing request identity; do not issue a new financial intent blindly.
The default `confirm(..., learn=True)` preserves the existing human-review trust
ramp. Routine execution and one-time answers do not create new learned rules.

`routine_context(entity)` returns bounded source-account, counterparty and
category options (at most 500 each), plus posted/held counts. These are data for
the host's policy UI. Current catalog-verified mappings and stored posted source
descriptors keep approved rules editable after their original sources leave the
worklist. A display-only account-type fallback never satisfies the planner's
required raw source-type check. Options are data for
the host's policy/question UI, not instructions or approval. The host remains
responsible for grouping questions, durable job state, roles, idempotent answers,
and fencing unrelated financial writes while its routine job runs.
