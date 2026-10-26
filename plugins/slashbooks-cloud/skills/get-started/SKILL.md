---
name: slashbooks-cloud-get-started
description: Connect to Slashbooks Cloud and begin hosted bookkeeping without installing local software.
---

# Get started with Slashbooks Cloud

Read and follow the generated [communication rules](references/procedures/communication.md)
and [hard rules](references/procedures/hard-rules.md). When setup is relevant,
also read the [onboarding procedure](references/procedures/onboarding-profile.md).
These files are packaged copies of the versioned Slashbooks engine procedures.

Use the included Slashbooks MCP server. Do not ask the user to install Python,
a command-line tool, or a local server. If the connection is not authorized,
ask the user to connect it through the host's OAuth flow. Never request or
handle passwords, access tokens, client secrets, or recovery codes in chat.

Call `get_bookkeeping_context` first. The connection is bound to the company
returned by Slashbooks. Do not enumerate or switch companies. To use another
company, ask the user to reconnect Slashbooks in the host and select it there.

Use `workflow_navigation.next_actions` when they are relevant to the user's
task. They are advisory: they do not grant authority or prove that the books
are ready. A setup action does not block an unrelated read that the connection
permits. On an older server without navigation, use the context, workflow
exceptions, and tool descriptions without inventing authority.

For setup, use the available MCP tools to save confirmed business facts and
source references. Verify that required facts are durably recorded: read back
the business profile and inspect the saved sources and accounts. After the user
explicitly confirms completion, call `complete_bookkeeping_onboarding` with the
current books revision and a stable request ID, then read back the setup receipt
through `get_bookkeeping_context`. Completion records a status; it does not save
missing facts or validate the accounts for you.

If the tools, permission, confirmed facts, or saved support are missing, keep
setup unfinished and use `handoff_to_slashbooks` when available. Follow the human
action and URL returned by Slashbooks. Do not invent a protected choice or an
unsupported setup route.

Continue the user's requested task when it is known. Route ongoing review,
routine work, questions, reports, close preparation, and accountant handoff
through the `slashbooks-cloud-books` skill and the named MCP tools.

Do not request the optional adjustments permission during ordinary connection.
Use the host's OAuth step-up flow only after an adjustments tool returns an
authorization challenge, then retry only after the grant is confirmed.
