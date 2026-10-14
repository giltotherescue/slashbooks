# Standing authority for bookkeeping preparation

`bookkeeping.automation_policy` provides a dependency-free preflight contract for
trusted engine integrations. `bookkeeping.preparation_runtime` applies that
contract to supported ordinary queue commands. Neither module authenticates an
executor by itself. Ordinary interactive commands retain their existing behavior.

A version-1 policy names its entity, bounded accounting periods, allowed
preparation actions and explicit source/entry holds. An execution context binds
that policy to a short-lived executor and an expected books revision. Changes to
the policy or books revision require a new preflight. Final approval, publication,
period close/reopen and authority changes cannot appear in a preparation policy.

The integration must obtain the policy and current version from its authenticated
authority store. Do not read authority from the agent's workspace, model output,
receipt text, question response or a caller-controlled environment variable.
Customer answers can supply facts; they cannot broaden a policy through this
contract. Authentication, revocation and per-operation commit checks remain the
integration's responsibility.

Build each `PreparationIntent` from validated engine state. Include every affected
period (both original and replacement periods for corrections), source and entry
IDs, explicit review holds, and fingerprints of the evidence actually used.
Fingerprints bind the decision to evidence; they do not establish its truth or
authorize replacement of a source-bound transaction. Existing accounting
validation, correction lineage and idempotency rules still apply.

`authorize_batch` returns immutable receipts only when every intent is permitted.
A mixed permitted/held batch raises `PreparationDenied` before returning any
receipt. This is an authority preflight, not a database transaction: the caller
must commit against the same revision and current policy, or reload and evaluate
again. Keep each receipt and its intent with the accounting effect in the same
transaction. Do not treat receipt replay as permission to repeat an effect.

The receipts contain policy version/fingerprint, executor, expected revision,
rule and intent fingerprint. They are suitable for accounting audit attribution,
but are not authentication tokens.

## Supported execution boundary

Call `supported_preparation_command(argv)` before routing a service command. It
returns an immutable positive-list classification with `read`, `proposal`,
`ledger`, or `reconciliation` effect. Only `read` commands may use ordinary
read-only dispatch; every listed effect must use `run_preparation_command`.
Unlisted commands fail closed. This prevents a service caller from accidentally
using the interactive confirmation route without standing-policy enforcement.

`run_preparation_command` accepts normal `ingest`, queue proposal, `queue
confirm`, `queue confirm-split`, `queue confirm-transfer`, and evidence-complete
single-account `reconcile` arguments, plus `ledger correct`,
`ledger reclassify-bank` and `ledger split-equity`. It calls the same dispatchers
as the CLI, with authority supplied by the embedding host. It does not read
`.env` or reroute to another remote workspace. Batch confirmations,
related-entity treatments, standalone reversals and other commands fail closed.

Imports require a trusted fingerprint for every normalized source row and reuse
that immutable input snapshot inside the ordinary ingest dispatcher. Proposals
require the currently staged source fingerprints and their corresponding policy
action. Automated reconciliation is limited to one account and requires the
opening balance, signed source activity, ending balance, snapshot time and
source-through date. It also requires one or more stable
`--source-evidence-ref` values. Its trusted fingerprint map contains
`reconciliation:<account>:<as-of>` for the exact numeric/timing payload and one
`evidence:<source-evidence-ref>` entry per independently verified provider
record or statement. The runtime never creates those attestations by hashing a
fresh model upload or model-provided balance. The host retains the operation
receipt and workspace revision guard for proposal and reconciliation metadata
writes.

The host supplies the expected ledger digest and fingerprints of previously
verified source records. The write path rechecks the digest inside the ledger
transaction, the proposal snapshot, the still-present staged sources and all
affected periods. Authority receipts commit or roll back with the entries.
Matched transfers check both source records before writing either side.
Missing staged sources cannot be reconstructed from a queue item's fallback
data in this mode. A previously posted source requires receipt reconciliation;
retrying preparation cannot create another economic effect.

Corrections require trusted `entry_hashes`, keyed by `entry:<SQLite id>` and
computed with `evidence_hash(dataclasses.asdict(entry))` from canonical entries.
The original entry and every correction ancestor must have a matching verified
fingerprint. The boundary checks the original is live, the reversal negates it,
and the replacement has a fresh source ID with the correct lineage. It includes
ancestor source IDs, paired source IDs, entry IDs and periods in the policy
decision, so a newer correction ID cannot bypass an older review hold. Receipts,
reversal and replacement share the ledger transaction. These commands require
the `correct` action; a source upload cannot substitute for original entry
evidence. The host may pass an empty `source_hashes` mapping for corrections.

An automated confirmation does not increment the learned-context human
confirmation count. Explicit holds still require human review. The host must
also enforce current revocation, closed periods, trusted evidence provenance and
the final workspace revision at publication/commit. This local transaction
boundary does not replace those checks. Hosted service integration and
qualification remain prerequisites for unattended use.

## Review readiness

`bookkeeping.readiness.assess_review_readiness` is a deterministic, read-only
engine interface. The host passes `DependencyVersions(books_revision,
source_revision, reply_revision, policy_version)` from authenticated live state,
plus an explicit evaluation date. The result names affirmative source coverage,
period-end reconciliations, unresolved staged work/proposals/duplicates/questions,
validated draft report references, exact engine totals and blockers. It requires
a completed period, a canonical ledger, source coverage dates, a ledger account
for every source, clean reconciliation with opening/activity evidence, and a
dated source snapshot through period end. Reconciliation records without stable
source evidence references and their verified fingerprints remain blocked.

The returned `package_id` binds the period, dependency versions, evidence
fingerprint, blockers and report references. The engine returns only `blocked`
or `ready_for_review`; the host derives `not_evaluated` and `stale` by comparing
the captured versions with live state. A zero exit code from `books readiness`
means only that an assessment was produced. The local CLI uses zero-version
dependencies for inspection and does not establish hosted authority. The host
must reject final human publication when any dependency version or package ID is
stale. No readiness result authorizes an agent to publish, close, or reopen.
