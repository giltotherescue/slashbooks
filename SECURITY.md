# Security Policy

Slashbooks is local-first bookkeeping software. Your books, bank API keys, and
other credentials live in your own company directory on your machine, not in
this repository or any hosted service. Keep credentials in a local `.env` file
or your OS credential store, and never commit them.

## Supported versions

Slashbooks is in beta (0.x). Security fixes are applied to the latest release.

## Reporting a vulnerability

Please report security issues privately, not in public GitHub issues.

- Preferred: use GitHub's "Report a vulnerability" button on the repository's
  Security tab (private vulnerability reporting).
- Include steps to reproduce, the affected version, and the impact. Do not
  include real credentials, bank data, or company financials in your report.

We will acknowledge the report, investigate, and coordinate a fix and
disclosure timeline with you. Please give us reasonable time to address the
issue before any public disclosure.

## Scope

In scope: vulnerabilities in this repository's code that could expose
credentials or financial data, corrupt the ledger or audit log, or let
untrusted input (transaction descriptions, CSV contents, web research) be
treated as instructions.

Out of scope: issues in third-party services you connect (for example BankSync,
Stripe, Mercury, or your AI agent), and the security of your own machine or
credential storage.
