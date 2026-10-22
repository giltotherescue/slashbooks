# Hosted directory plugin package

`plugins/slashbooks-cloud/` is the hosted-only Slashbooks package. It connects to
`https://slashbooks.co/mcp` and has no local runtime. It is separate from the
repository-root plugin, which continues to package the open-source engine and
command-line workflow.

## Package formats

- OpenAI's portable Agent Plugins format uses the root `plugin.json`, `mcp.json`,
  and `skills/` folder. The MCP declaration uses streamable HTTP and OAuth with
  dynamic client registration; discovery supplies the OAuth endpoints. Its
  ordinary consent requests the final release's seven default connector scopes.
  `adjustments:write` is omitted and must be requested through OAuth step-up only
  when the user asks for an adjustments operation.
- Claude's plugin bundle format uses `.claude-plugin/plugin.json`, `.mcp.json`,
  and the same `skills/` folder. Claude directory publication of this bundle
  requires a public GitHub repository and a portal submission.
- Claude also accepts a remote MCP connector submission that points directly to
  the hosted endpoint. That connector is distinct from the GitHub plugin bundle.

Official references checked on 2026-10-22:

- OpenAI, [Package your plugin](https://developers.openai.com/plugins/build/plugins)
- OpenAI, [Upload and submit your plugin](https://developers.openai.com/plugins/deploy/submission)
- Anthropic, [Publish to the directory](https://claude.com/docs/directory/publish)

The public OAuth discovery document currently advertises dynamic client
registration, authorization-code and refresh-token grants, PKCE S256, and a
public-client token method. An unauthenticated request to the MCP endpoint returns
`401` with the protected-resource metadata URL. The deployed discovery document
still advertises the older four-scope baseline. This package follows the complete
release contract in `hosted/worker/src/cloud/connector-config.ts`: those four
scopes plus `sources:write`, `runs:write`, and `books:write` by default, with
`adjustments:write` available only through step-up. Deployment and signed-in
acceptance therefore remain release gates.

## Publication gates

This source package is not a submission-ready archive. Before submission:

1. The publisher must approve the listing copy and final `/books` artwork.
2. Add approved square icons to the package and reference them in the manifest.
3. Finish and deploy the directory release of the MCP server, then run the review
   cases against the deployed endpoint and record actual outcomes.
4. Complete the portal's domain, OAuth, reviewer-access, country, commerce, demo,
   identity, and organization-ownership fields with verified facts.
5. Build and inspect a clean archive from the reviewed commit. Do not include
   local files, secrets, dependencies, hooks, or app bindings.

The package metadata is draft publication copy. A valid package does not prove
that a portal has accepted its endpoint, identity, OAuth flow, tools, or listing.
