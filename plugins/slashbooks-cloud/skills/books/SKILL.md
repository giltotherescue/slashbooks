---
name: slashbooks-cloud-books
description: Use Slashbooks Cloud for bookkeeping review, questions, reports, close preparation, and accountant handoff.
---

# Work with hosted Slashbooks books

Read and follow the generated [communication rules](references/procedures/communication.md)
and [hard rules](references/procedures/hard-rules.md). Read the additional
procedure that applies to the task:

- setup: [onboarding](references/procedures/onboarding-profile.md)
- open questions: [question rules](references/procedures/question-rules.md)
- routine work with blockers: [non-blocking work](references/procedures/non-blocking.md)
- close or readiness: [month-end check](references/procedures/month-end-check.md)

These files are packaged copies of the versioned Slashbooks engine procedures.
Use only the named tools exposed by the included Slashbooks MCP server. The
hosted service is authoritative for company access, bookkeeping state, basis,
currency, periods, approvals, and saved results. Do not use a local Slashbooks
installation or calculate accounting results in the conversation.

Call `get_bookkeeping_context` at the start of a bookkeeping workflow and after
a relevant state change. The connection is bound to the returned company. To
use another company, ask the user to reconnect Slashbooks in the host.

Use `workflow_navigation.next_actions` when relevant to the user's task. The
actions are advisory and do not grant authority or prove readiness. A setup
action does not block an unrelated permitted read. On an older server without
navigation, use the context, workflow exceptions, and tool descriptions without
inventing authority.

For **Update my books**:

1. Use the exact current approved routine policy. Call
   `start_authorized_bookkeeping` only when the request and policy cover it.
2. Refresh a source only when the task needs current data, the source is stale,
   and Slashbooks permits the action.
3. Follow the run with `get_bookkeeping_work`. Keep open questions and
   exceptions visible.
4. Use `check_close_readiness` only when the task needs a readiness claim.

Use the question tools for open questions and the report tools for financial
answers. Use the company, period, basis, currency, and scope returned by
Slashbooks. For setup, follow the returned human action and URL; the current
hosted tools cannot prove all durable prerequisites, so do not select or call onboarding completion.

Read state before proposing work. Continue routine actions already allowed by
the current approved policy and authenticated authority. Stop for a person-only
decision. If an adjustments tool returns an authorization challenge, use the
host's OAuth step-up flow and retry only after the grant is confirmed.

For reviews and handoff, separate confirmed facts, proposed treatment, open
questions, exceptions, and completed actions. Do not claim a write, export,
reconciliation, or readiness result unless the tool confirms it.
