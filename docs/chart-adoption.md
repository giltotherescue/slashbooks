# Adopt a source chart

`books entity chart-adopt COMPANY --input reviewed-chart.json` previews an
explicitly mapped chart. Add `--apply --plan-hash HASH` using that preview to
apply it. This command adds missing accounts and preserves source evidence. It
never deletes accounts, rewrites posted entries, renames a ledger key or imports
balances. It is not yet a display-label rename or a full QuickBooks migration.

The JSON manifest has exactly these fields:

```json
{
  "version": 1,
  "operation_id": "owner-chart-1",
  "source": "reviewed-export",
  "decision": "preserve",
  "scope": "partial",
  "open_date": "2026-01-01",
  "accounts": [
    {
      "source_id": "42",
      "name": "Design & illustration",
      "number": "4100",
      "parent_id": null,
      "type": "Income",
      "account": "Income:Design"
    }
  ]
}
```

`decision` records preserve or rebuild. Rebuild means the person explicitly chose
new mappings for this source, not permission to delete existing books. `scope`
is complete or partial and describes the input, not a replacement instruction.
A screenshot is usually partial: do not infer missing accounts, numbers or types.
Resolve unclear extraction with the person before preparing the manifest.

`source_id` is stable within `source`. `name`, `number` and `parent_id` preserve
exact source text and relationships. Numbers may be null or duplicated; duplicate
numbers are reported, not conflated into account identity. `account` is an
explicit valid ledger key whose root matches `type` (Assets, Liabilities, Equity,
Income, Expenses). Source names are not silently sanitized. The preview always
shows both the source name and the mapped key. Two source accounts cannot map to
one key in a manifest; that would imply a merge, which this command does not do.
All parent rows must be included and cycles are refused. At most 1,000 accounts
and 2 MB are accepted per manifest.

Zero-balance accounts and income/expense accounts are created as catalog entries
without postings. Existing accounts are kept unchanged. An existing source ID
cannot be reassigned or relabeled through adoption. Reusing an operation ID with
a changed manifest fails. Replaying an applied identical operation returns its
receipt. A changed ledger requires a fresh preview.

The catalog, source evidence, decision record and audit seal commit in one SQLite
transaction. Source evidence is in the sealed `meta.chart_adoption_v1` JSON;
this initial command does not change customer report/export labels or the tax
handoff account-number metadata. Those reader integrations are separate work.
Adopted accounts are protected from later starter-chart selection and unused
account removal so an owner preference cannot disappear silently.

Inspect the active catalog together with source evidence using
`books entity chart-inspect COMPANY`. The result keeps `account` ledger keys
separate from source `name` and `number`; consumers must not confuse either
with a rename already applied to reports.

Preserve requires each mapped child to stay under its mapped parent. Rebuild
may choose another hierarchy, while retaining the original source parent in
evidence. Different source charts may map to the same ledger key only when
their exact source name, number and type agree; conflicting presentations need
a separate decision, not last-writer wins.
