# Developer Handoff Format

Use this format for the final message. Omit a section only when it would add no
information. Do not add a file-by-file implementation plan unless the feedback
already contains a verified technical pointer.

```md
# Slashbooks feedback: <clear outcome or problem>

**Type:** <correctness | workflow | agent usability | reliability | token efficiency | documentation>
**Priority:** <P0–P3>
**Confidence:** <high | medium | low>

## Summary

<One or two sentences: what should work better and for whom.>

## What we observed

- <Fact, exact output, or redacted example.>
- <Scope: source, workflow, date range, or conditions.>

## Why it matters

<Business, accounting, or agent-workflow impact. State uncertainty when it remains.>

## Desired behavior

- <Observable result.>
- <Any important boundary; for example, preserve a review decision rather than auto-posting.>

## Suggested investigation

- <Optional. A verified code path, report, or comparison point.>
- <Say `Unknown` rather than guessing at a root cause.>

## Acceptance checks

- <A testable behavior or invariant.>
- <A relevant regression or data-safety check.>

## Constraints and open questions

- <Only unresolved facts, privacy constraints, or deliberate non-goals.>

## Developer note

Verify the source behavior and tests before implementing. This feedback does not
authorize changes to real company data, automatic approval of uncertain
transactions, external messages, releases, or deployments.
```

## Triage guide

Use the lowest priority that honestly describes the impact:

- **P0:** May produce a materially incorrect ledger, report, export, or unsafe
  data action.
- **P1:** Blocks a normal bookkeeping workflow or produces misleading guidance
  that needs prompt correction.
- **P2:** Causes repeated manual work, poor agent decisions, or unnecessary
  token/tool use, with a safe workaround.
- **P3:** A useful polish, clarity, or documentation improvement.

## Writing rules

- Lead with the observable outcome, not the suspected implementation.
- Quote exact error text only when it changes diagnosis. Redact company-specific
  names, identifiers, amounts, and paths unless they are essential and approved
  for the handoff.
- Use a source type (for example, `Mercury`, `Stripe`, `QuickBooks export`) and
  a date range instead of pasting transaction data.
- State which result is expected. “Make it better” is not an acceptance check.
- Keep a user preference separate from an accounting fact. A developer may need
  to make both configurable rather than hard-code one company’s convention.
- Do not call an unverified hypothesis a root cause. Put it in `Suggested
  investigation` and label it as a hypothesis.
