# Slashbooks Cloud plugin package

This package connects supported AI hosts to the hosted Slashbooks MCP endpoint at
`https://slashbooks.co/mcp`. It contains instructions and connection metadata. It
does not contain the Slashbooks engine, a local executable, Python dependencies,
credentials, or private service identifiers.

The root `plugin.json` and `mcp.json` are the portable Agent Plugins package used
by OpenAI hosts. `claude-plugin.template.json` and `.mcp.json` are the source for a
future standalone Claude plugin bundle. Place the template at
`.claude-plugin/plugin.json` in that separate submission package. Do not put a
second `.claude-plugin/plugin.json` under this repository: Claude's marketplace
sync treats it as a nested plugin and rejects the repository's local Slashbooks
plugin. The open-source, local Slashbooks package at the repository root remains
a separate installation.

This folder is source material for review. It includes the approved `/b` icon and
a preview of the owner-approved listing direction. The exact final listing packet
still requires publisher review before a public submission.

For Claude's public directory, the remote MCP connector and this plugin bundle
are separate submissions from the same organization. Pair them in the listing:
the connector provides the tools and authorization, while this bundle provides
the workflow skills. Publishing either submission does not publish the other.
