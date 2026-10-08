# Local Books and Slashbooks Cloud

Slashbooks is one plugin and one `books` CLI. A company's books live in one of
two places, chosen during onboarding:

| | Local books | Slashbooks Cloud |
| --- | --- | --- |
| Where the books are | A folder on your computer (`entity.json`, `ledger.sqlite`) | Slashbooks servers at slashbooks.co |
| Cost and license | Free and open source | Hosted service with an account |
| Who works on them | You, through your AI agent | You and your accountant or firm, through the agent or the website |
| Backups | Yours to manage | Automatic |
| Bank connections | Optional BankSync, Stripe, Mercury or CSV, with your own keys | Set up on the website |

Both use the same commands, skills and accounting rules. Local books can move to
the cloud later; see [Move existing local books to a hosted company](hosted-migration.md).

## How the CLI selects local or cloud

A company folder that contains `.slashbooks-remote.json` is linked to cloud
books. Every ordinary `books` command run against that folder goes to the
cloud company. A folder without that file uses local books, exactly as before.

- The CLI never falls back to local books after a cloud error. It stops, keeps
  the link, and reports the error.
- A missing local `entity.json` in a linked folder does not mean the books need
  setup. The books are on the server.
- Commands that take `--entity` use that folder's link. Commands without that
  option, such as `qb inventory`, use the link in the current directory.

## Connect a folder to cloud books

An accountant opens the company on slashbooks.co, then **Agent setup**, and gives
the connection message to the agent. The agent then runs:

```sh
books hosted login --company <company-id> --entity <folder>
```

The command prints a sign-in link and a code. The user opens the link, checks
the company name and code, and chooses **Allow access**. The agent never sees a
password or key. The credential is stored in an owner-only file in
`~/.config/slashbooks/agents/`, outside the company folder. `--endpoint`
defaults to `https://slashbooks.co`; pass it only for another server.

To reconnect the same company after access expires, use `--reauthorize`. A
folder linked to one company is never relinked to a different company.

## Shared company files

Cloud books keep the business profile, configuration and notes on the server:

```sh
books hosted file list --entity <folder>
books hosted file get business-profile.md --output <folder>/scratch/business-profile.md --entity <folder>
books hosted file put business-profile.md --file <folder>/scratch/business-profile.md --entity <folder>
```

`file put` uses the company revision recorded by `file get`. If another change
came first, the put fails; get the file again and reapply the change. Only
profile, configuration and notes files can be written this way. Ledgers, queues,
learned rules, source exports and secrets cannot.

## Bank and provider data

For a linked company, a provider command uses a local provider key if one is
set; otherwise it uses the provider connection configured for the cloud
company. QuickBooks browser exports and custom ingestion scripts stay local:
their output goes to the cloud company through the ordinary `books ingest` and
import commands.

## Errors and retries

The CLI saves a receipt for each cloud write in `<folder>/.slashbooks-remote-receipts/`.
If the result of a write is uncertain, retry it with the same receipt, which
reuses the original request and idempotency key:

```sh
books hosted retry --receipt <folder>/.slashbooks-remote-receipts/<receipt>.json
```

Do not rerun the command with a new key. After a version conflict, read the
current state again and decide again.
