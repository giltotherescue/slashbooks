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

`run_preparation_command` accepts normal `queue confirm`, `queue confirm-split`
and `queue confirm-transfer` arguments, plus `ledger correct`,
`ledger reclassify-bank` and `ledger split-equity`. It calls the same dispatchers
as the CLI, with authority supplied by the embedding host. It does not read
`.env` or reroute to another remote workspace. Batch confirmations,
related-entity treatments, standalone reversals and other commands fail closed.

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
boundary does not replace those checks. Broader command coverage, hosted
service integration and qualification remain prerequisites for unattended use.
