---
name: mail-triage
version: 1
title: Mail triage
---
- Fetch the released incoming messages through the service mail interface. If the environment has no mail interface, say so. Do not invent one.
- Read each message and every listed attachment with the ordinary file commands.
- Decide how each message relates to the books. Match receipts and invoices to transactions. Find duplicates and irrelevant messages.
- Use a factual answer within standing authority. Ask a scoped question when facts conflict or are not enough.
- After each message, record exactly one disposition: `recorded` with a committed command ID, `needs_answer` with an open question ID, or `no_action`.
- Write a short plain-language summary with each disposition. State what was applied, what remains, and the missing fact or blocker for each remaining part.
- Do not put command IDs or implementation terms in the summary. A customer can read it.
- Record a disposition only after the matching accounting command or question succeeds. Do not mark an uncertain or unprocessed message complete.
- Fetch the mail again after the batch. Continue until no actionable mail remains or the run budget needs a checkpoint.
