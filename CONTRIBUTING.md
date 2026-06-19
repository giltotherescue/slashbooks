# Contributing

Thanks for helping improve Slashbooks.

## How It Fits Together

Slashbooks has three layers: agent **skills** (the `/books-*` workflows) drive
the **`books` CLI**, which does all deterministic accounting and writes the
**local ledger files** (`books.beancount`, the audit log, and the review queue).
The agent is the interface; the CLI is the engine. See
[docs/philosophy.md](docs/philosophy.md) for the reasoning behind that split.

## Safety First

Do not commit API keys, `.env` files, bank exports, QuickBooks exports, company
ledgers, generated reports, or entity directories.

Use real provider credentials only in a local company books folder, never in this
plugin repo. Provider-specific helpers that only apply to one company should live
in that company's books project under `ingestion/custom/`.

If a real credential is ever committed or shared publicly, rotate it right away.

## Secret Scanning

This repo runs Gitleaks in CI to block accidentally committed API keys,
credentials, and private company data. To run the same check before commits:

```sh
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

## Local Setup

```sh
git clone https://github.com/giltotherescue/slashbooks slashbooks
cd slashbooks
pip install -e .
python3 -m unittest discover -s tests
```

The public command is `books`.

## Before You Push

Run the test suite:

```sh
python3 -m unittest discover -s tests
```

Before publishing plugin changes, also run:

```sh
claude plugin validate --strict .
```

For Codex plugin validation, use the validator from the local `plugin-creator`
skill.

## Connectors

A source connector gets outside financial data into Slashbooks.

Common connector types:

- Provider API connectors, such as Stripe and Mercury.
- Aggregator connectors, such as BankSync.
- File connectors, such as CSV and QuickBooks exports.

To add a reusable connector:

1. Create `src/bookkeeping/connectors/<source>.py`.
2. Register the CLI subcommand in `src/bookkeeping/cli.py`.
3. Declare any source config shape in `entity.json`.
4. Add tests for parsing, normalization, and account mapping behavior.

Keep API downloads and ledger posting separate unless the accounting treatment is
deterministic and tested.

## Skills

Workflow skill names are prefixed, such as `/books-close`, so they remain
portable when copied into `.agents/skills`, `.codex/skills`, `.claude/skills`,
or another Agent-Skills-compatible library without plugin-level namespacing.

## Chart Of Accounts Templates

To add a chart-of-accounts template, add a template under
`skills/books-onboard/templates/`, register the business type in
`src/bookkeeping/entity.py`, and keep onboarding copy-once and non-destructive.
