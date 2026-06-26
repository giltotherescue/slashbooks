# Contributing

Thanks for helping improve Slashbooks.

## How It Fits Together

Slashbooks has three layers: agent **skills** (the `/books-*` workflows) drive
the **`books` CLI**, which does all deterministic accounting and writes the
**local company books** (`ledger.sqlite`, the review queue, and generated reports).
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

## Local Demo Books

For local development, use editable install plus a synthetic demo company. This
lets you test the current checkout's CLI without installing the plugin through
an app marketplace or publishing a release:

```sh
pip install -e .
books demo init ~/Documents/books/northstar-demo
cd ~/Documents/books/northstar-demo
books report pnl --entity . --from 2026-01-01 --to 2026-12-31
books queue list --entity .
```

The demo company is fictional and safe to delete/recreate. It includes
`ONBOARDING.md`, a SQLite ledger/account catalog, learned context, and open
review items. Do not create demo books inside this source repository.

## Local Agent Plugin Testing

To test the current checkout through an agent, install the local plugin
marketplace instead of the public GitHub marketplace. The skills call their
bundled `scripts/books` wrapper, so agent testing does not depend on a global
`books` command on `PATH`.

Claude Code:

```text
/plugin marketplace add /path/to/slashbooks
/plugin install slashbooks@slashbooks
```

Then open Claude Code in the demo company directory and run `/books`:

```sh
claude ~/Documents/books/northstar-demo
```

Codex:

```sh
codex plugin marketplace add /path/to/slashbooks
codex -C ~/Documents/books/northstar-demo
```

After adding the local marketplace, restart Codex if needed and install/enable
`slashbooks` from the local Slashbooks marketplace. The local marketplace points at
this checkout, so edits to `skills/`, `.codex-plugin/`, and the Python package
can be tested before release.

After changing the local plugin, refresh Codex's installed cache:

```sh
scripts/refresh-codex-local-plugin
```

The refresh registers this checkout as the local marketplace, syncs the current
checkout into Codex's installed plugin cache, and opens the plugin detail page
when the Codex app needs to install or update it. You do not need to bump the
plugin version for local development. Start a new Codex thread after refreshing.

If the local marketplace does not appear in the Codex app plugin list, open the
local plugin detail page directly:

```sh
SLASHBOOKS_REPO=/path/to/slashbooks
MARKETPLACE_URL=$(python3 - <<'PY'
import os
from urllib.parse import quote
print(quote(os.path.join(os.environ["SLASHBOOKS_REPO"], ".agents/plugins/marketplace.json"), safe=""))
PY
)
open "codex://plugins/slashbooks?marketplacePath=${MARKETPLACE_URL}"
```

In the Codex CLI, run `/plugins` and switch to the `slashbooks` marketplace.

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

## Account Catalog Starters

To add an account-catalog starter, update the starter account definitions in
`src/bookkeeping/entity.py`, register the business type there, and ensure
`books entity init` seeds `ledger.sqlite` without requiring an entity-local
Beancount chart file.
