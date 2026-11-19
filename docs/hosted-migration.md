# Move Existing Local Books to a Hosted Company

This is an agent/operator procedure for an existing Slashbooks company, not new
company initialization or a QuickBooks opening-balance import. The normal local
workflow is unchanged. These commands describe the current implementation; this
guide does not establish deployment, production acceptance, or customer readiness.
Confirm that the selected server supports migration before starting.
Agents perform the commands and checks, then present a concise result; accountants
do not need to execute this checklist manually.

## 1. Confirm Authority and an Empty Target

Obtain explicit approval for the source company, hosted destination, and handoff.
Use an authorized staff account to create a company through the hosted UI or
`POST /api/v1/companies`. Grant the intended accountant/client memberships and
create a company-scoped agent key with `books:read` and `books:write`.
Do not borrow another company's key or a browser session to bypass a denial.

The target must have `books_revision: 0`, matching cash- or accrual-basis books in a matching currency, no closed period,
and no existing engine workspace or bookkeeping records. A starter account
catalog from company creation is allowed. Do not run `books entity init`, demo
seeding, opening imports, or other bookkeeping writes against this target first.
The server rechecks emptiness at commit. Never clear an occupied company to make
it eligible; stop and resolve the destination with the authorized operator.

Keep the source outside the plugin repository. Pause source writers, including
other agents and scheduled imports. Keep writes paused through comparison and
the explicit handoff so new local work is not left behind by the snapshot.

## 2. Make an Offline Snapshot

Use a new private working directory outside the source. The paths below are
placeholders; confirm them before execution. Keep all artifacts private because
bundles, receipts, reports, and downloads contain company data.

```sh
umask 077
SOURCE="$HOME/Documents/books/existing-company"
WORK="$HOME/Documents/books/hosted-migration-check"
mkdir -m 700 "$WORK"
VERIFY="$WORK/verify"
mkdir -m 700 "$VERIFY"
CONFIG="$VERIFY/.slashbooks-remote.json"

books migration export --entity "$SOURCE" --output "$WORK/company.zip"
```

`books migration export` is the fixed, network-free engine command. A hosted
executor can run the same command with `--entity @entity` and
`--output @output/company.zip`; those are the only company and output path
fields in its remote command contract. `books cloud migration export` remains
the local transport-management alias for compatibility.

Accrual bundles use the supported version-2 format and retain evidence, intents,
schedules, items, allocations, coverage, and periods; legacy cash bundles remain
supported. The destination must support the source capability and schema. Do not
downgrade an accrual store to a cash-only writer. See [accrual scope](accrual.md).

Export is offline: it captures a coherent SQLite snapshot, inventories supported
files and references, checks ledger/audit integrity, and records financial-report
fingerprints. It does not initialize or modify the source, contact the server,
or migrate the company. Retain the source and its before/after file hashes.

Require a successful exit and `bundle_created: true`. `import_ready: false` in
an export summary is not proof of failed export; export alone never establishes
remote acceptance. A blocked export is a stop condition. Preserve its diagnostic
codes and original files. Do not remove history, queues, reports, documents, or
custom references merely to obtain a passing export.

## 3. Configure Only the Verification Directory

The authorized operator supplies `HOSTED_ORIGIN` and `COMPANY_ID`. Supply the
company key securely as `BOOKS_API_TOKEN` through the environment or a secret
manager, not chat, command arguments, source files, or documentation.

```sh
books cloud configure --endpoint "$HOSTED_ORIGIN" --company="$COMPANY_ID" --entity "$VERIFY" --tokenref env:BOOKS_API_TOKEN
books cloud --config "$CONFIG" status
books cloud --config "$CONFIG" context
books cloud file list --entity "$VERIFY"
```

Use a trusted HTTPS origin. `--allow-localhost` is only for explicitly authorized
local development. Configuration saves settings; it does not authenticate,
create a company, or move books. Verify the returned identity, access, revision,
and empty state before importing. Global `--config` and `--timeout` options go
before the hosted subcommand.

Do not bind or reconfigure `SOURCE`. This separate directory is for deliberate
remote verification, not permission to switch the working agent or resume writes.

## 4. Import Once With a Business Explanation

```sh
books cloud --config "$CONFIG" --timeout 180 migration import --bundle "$WORK/company.zip" --explanation "Move the approved existing company history into its authorized empty hosted workspace for shared bookkeeping."
```

Use a specific, truthful explanation of the approved move, without credentials.
The CLI generates an idempotency key and saves an immutable intent plus an
adjacent private frozen `.bundle` before sending the request. An explicit
`--idempotency-key` is supported when the operator has already retained that key.
After an uncertain result, never start a new import or use a fresh key; reconcile
or retry the saved receipt as described below.

Success output has `{idempotency_key, local_receipt, response}`. Inspect the
`response`, not just the outer envelope. Require `state_committed: true`,
`books_revision: 1`, the expected source/manifest hashes, `validation_summary`,
ledger/report `validation_anchors`, complete `id_mapping`, and registered
`documents` records shaped as `{id, path, size, sha256}`.

## 5. Verify Before Handoff

Use the returned command ID and the source manifest's report period. Set
`COMMAND_ID`, `FROM`, and `TO` from that private evidence, not guesses.

```sh
books cloud --config "$CONFIG" command-result "$COMMAND_ID"
books cloud --config "$CONFIG" entries --all
books cloud --config "$CONFIG" proposals --all
books cloud --config "$CONFIG" history --all
books cloud --config "$CONFIG" evidence --all
books report pnl --entity "$VERIFY" --from "$FROM" --to "$TO" --format json
books report balance-sheet --entity "$VERIFY" --as-of "$TO" --format json
books report trial-balance --entity "$VERIFY" --as-of "$TO" --format json
books report general-ledger --entity "$VERIFY" --from "$FROM" --to "$TO" --format json
```

- Match the persisted receipt, ledger content/table hashes, core audit anchors,
  entry IDs/postings, and review state to the exported source. Check that old
  history has not acquired the importing agent's authorship.
- Compare all four reports for the same period using canonical JSON fingerprints,
  not screenshots or rounded totals. Ordinary `books report` must use the
  verification binding, not silently read the original local ledger.
- The bundle's report fingerprints use the statement layout from before earlier
  fiscal years were closed into retained earnings in every view. In that layout
  the balance sheet of a cash company has one cumulative net income line (an
  accrual company keeps the accrual view's split, as before), and the trial
  balance shows life-to-date income and expense accounts in every view. A bundle
  made by an earlier version still validates. For a company with more than one
  fiscal year, the `books report balance-sheet` and `trial-balance` JSON
  therefore differ from the fingerprinted layout. On the balance sheet of a cash
  or recorded view, the retained earnings and net income lines differ and `meta`
  gains `fiscal_start`, `cumulative_net_income` and `current_fiscal_net_income`;
  every total is the same. On the trial balance (every view), income and expense
  rows show the current fiscal year, a computed retained earnings row is added,
  `meta` gains `fiscal_start`, and total debits and total credits are lower by
  the same amount. The ledger and its entries are the same.
- Check shared `entity.json`, business profile, and context through cloud file
  access. Download each registered document through the authenticated evidence
  download route, `GET /api/v1/companies/:company/evidence/:id/download`, and compare
  size and SHA-256 to its manifest entry. Evidence listings must retain
  `original_path` and `source: local_migration`; a listing alone is not byte parity.
- Verify the intended accountant/client access and cross-company denial. Confirm
  an authorized ordinary read and a controlled context operation still work after
  adoption. Do not create a financial correction just to make this check pass.
- Recheck that the source files are unchanged. Retain the original company,
  snapshot, receipt and frozen receipt bundle, and private parity evidence.

After these checks pass, the original authorization covers moving the working
agent to the verified remote binding. Ask again only if the destination or scope
changes, or a comparison fails. Choose one authoritative writer; this is not a
two-way synchronization workflow. Do not delete or reinitialize the local source.
Any later source changes require reconciliation, not another blind adoption.

## Recover an Uncertain Result

On timeout, connection failure, invalid response, or failure to save the response,
stop writes and keep the exact `local_receipt` path reported by the CLI. Keep its
adjacent `.bundle`, original configuration, credential reference, and credential
identity. Do not edit the receipt or frozen bundle, change the explanation,
generate a fresh key, or select another company to bypass a conflict.

```sh
books cloud --config "$CONFIG" --timeout 180 retry --receipt "$RECEIPT"
```

Set `RECEIPT` to the exact retained path. A pending receipt resubmits the same
frozen request with its original key. A received receipt validates and returns
the saved response locally; that alone is not a fresh server observation. Use
`command-result` and current company reads to reconcile persisted state, then
complete parity checks. If the receipt or identity cannot be restored, escalate
with private evidence instead of creating a replacement write.

## Current Boundaries

- Migration requires a supported canonical SQLite schema, matching basis, a matching company currency (`migration_bundle.require_target_currency`),
  a valid core audit chain, and an empty remote target. It does not convert basis,
  currency, unsupported schema, or accounting policy.
- Current file limits are 32 MiB per file and 64 MiB for captured workspace bytes,
  with fewer than 4,096 bundle files and bounded inventory/transport metadata.
  Identity mapping is limited to 10,000 entries/proposals/sources combined.
  Compression does not bypass these limits. Server limits may reject a bundle
  even when local export succeeded.
- Only supported company files and validated portable references are accepted.
  Missing, external, absolute, unsafe, or excluded active references and custom
  executable/provider configuration can block export. No arbitrary custom code
  is executed remotely. Do not rewrite live source references without a separate
  reviewed plan; a passing file inventory is not proof of every custom workflow.
- Local related-entity policy does not authorize access to another hosted
  company. Resolve any policy/reference validation blockers without discarding
  the policy or its history.
- Provider credentials, bank consents, live connections, and external provider
  resources are never copied or established by migration. Historical downloaded
  source data is distinct from a working connection. Arrange and verify any
  authorized provider setup separately; never claim a migrated bank connection
  from preserved source identifiers.

See [local books and online books](cloud.md) for ongoing remote commands and
file-update rules. Existing local-only onboarding remains unchanged.

## Checks for each write

A host that runs the engine for each write can check the change, not the whole book. Each check below costs
time in proportion to the rows that the write added. Run the full checks on a schedule.

| Guarantee | Per write | Scheduled |
| --- | --- | --- |
| No posted row was rewritten or deleted | The append-only triggers refuse it in the database. `append_only_guard_problems(conn)` reads a fixed number of catalog rows to confirm that the triggers are in place. | `content_digest` against the seal (`check_store_integrity`) |
| The audit chain is whole | `verify_audit_chain_since(None)` once, then `verify_audit_chain_since(head)` after each write; keep the returned `head` only when `errors` is empty. | `verify_audit_chain()` from the first event |
| New postings name an existing entry | `posting_watermark()` before the first write, then `check_new_postings(mark)` after each write; keep its `watermark` for the next write. | `PRAGMA foreign_key_check` |
| The file is not corrupt | No cheap check exists. | `PRAGMA integrity_check` |

The incremental chain check does not read events at or before the head that the host passes. A change
below that head is stopped by the triggers. A person with direct access to the database file can drop a
trigger, so a scheduled run still needs the full checks. The seal digest also needs a read of the whole ledger,
so the per-write check cannot replace it. `PRAGMA integrity_check` and `PRAGMA quick_check` read every page, so
they have no incremental form.

The triggers also refuse a replace (`REPLACE INTO`, `INSERT OR REPLACE`, an upsert) and any new row with an id that is
not above every earlier id. So a saved head or watermark cannot skip a new row. Each check takes its new
checkpoint from the rows that it read in one read transaction. A row that another writer adds later is
checked by the next call.

### When an incremental check can stand in for the full check

- Use a head or a watermark only if an error-free check on the same file returned it. Never make one up and
  never reuse one after a restore, a copy or a migration. `check_new_postings` gives an error, and no watermark, when
  the watermark is above the newest posting id, or when the posting row that it names is missing or changed. The
  watermark holds the posting id and a hash of that row and of its entry. Even so, reset both checkpoints with the
  full checks after any restore.
- The append-only triggers must be present the whole time. Run `append_only_guard_problems` with each check.
- A trigger can have more than one valid text when the texts refuse exactly the same writes (for example, a text
  that lets SQLite use an index). `append_only_guard_problems`, the migration check and the engine accept each
  valid text. Any other text is a problem. The engine installs a missing, changed or earlier trigger text the
  first time that it opens a store for writing, in one transaction under the
  company write lock, and it writes no row, no audit event and no seal (the triggers are not part of
  `content_digest`).
- `meta`, `accounts` and `source_transactions` have no triggers. `books entity account-rename` relabels posted
  rows. The relabel is allowed only while its hash-chained `account-renamed` event is the newest audit event,
  and that event names the exact old and new account. When a new `account-renamed` event is in the links that
  `verify_audit_chain_since` reads, the result has `full_check_required=True` and no head. Run the full checks
  (`verify_audit_chain`, the seal digest in `check_store_integrity`), then call `verify_audit_chain_since(None)`
  for a new head, and reset the posting watermark with `posting_watermark()`.
- The incremental checks never cover the seal digest, `PRAGMA integrity_check`, `meta`, `accounts` or
  `source_transactions`. Run those on a schedule.
