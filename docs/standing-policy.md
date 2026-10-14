# Standing authority for bookkeeping preparation

`bookkeeping.automation_policy` provides a dependency-free preflight contract for
trusted engine integrations. It does not enable unattended execution by itself.
Ordinary interactive commands retain their existing behavior.

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
but are not authentication tokens. CLI/queue/importer integration and hosted
commit enforcement must be completed before unattended preparation is enabled.
