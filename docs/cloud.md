# Local Books and Slashbooks Cloud

Company creation explicitly selects cash (default) or supported accrual basis.
A cloud company uses the same engine contract as local books after that. A person
can turn accrual on later (`books accrual enable`) and change the default report
view (`books entity set-view`); both are previews until a person applies them, and
standing posting authority never covers them. When the company has no entries and
no person set a currency, the first import that names a currency sets the company
currency, and the host saves
`entity.json` with the ledger (see [company currency](company-currency.md)).
Accrual adjustments use the same engine
plans as local books and require explicit `adjustments:write` delegation;
client-only access stays read-only. Period publication/locking uses the existing
separate authority boundary. See [accrual books](accrual.md).

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

## Windows setup

Use the plugin with Python 3.11 or newer installed on Windows. No WSL, extra
Python package, or administrator session is required. The agent uses the `.cmd`
launcher in PowerShell or Command Prompt, and the shell launcher in Git Bash.
Browser approval saves a private credential and the company connection locally,
just as it does on macOS and Linux.

Windows credentials use a protected access-control list granting only the
signed-in Windows user access. File locks serialize credential renewal and
company changes. Private storage is checked before starting browser sign-in;
`PRIVATE_STORAGE_UNAVAILABLE` means the user profile needs a writable local
folder with Windows permissions, such as an NTFS drive. Do not bypass the check
or copy credentials into a shared folder. Existing insecure files are refused,
not silently changed.

A temporary sign-in service error returns `AUTH_SERVICE_UNAVAILABLE`. Try again
later. It does not mean access was declined. Unexpected error responses return
`AUTH_RESPONSE_INVALID`; neither message includes the server response body.

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

An accountant or firm staff member asks the agent to connect, and the agent runs:

```sh
books cloud login
```

The command prints a sign-in link and a code. The user opens the link, checks
the code, chooses the company, and chooses **Allow access**. The CLI then links
the folder `~/Documents/Slashbooks/online-<id hash>` and prints its path. To use
a known company or folder, pass `--company <company-id>` (from **Agent setup** on
slashbooks.co) or `--entity <folder>`. The agent never sees a
password or key. The credential is stored in an owner-only file in
`~/.config/slashbooks/agents/`, outside the company folder. `--endpoint`
defaults to `https://slashbooks.co`; pass it only for another server.

To reconnect the same company after access expires, use `--reauthorize`. A
folder linked to one company is never relinked to a different company.

## Shared company files

Cloud books keep the business profile, configuration and notes on the server:

```sh
books cloud file list --entity <folder>
books cloud file get business-profile.md --output <folder>/scratch/business-profile.md --entity <folder>
books cloud file put business-profile.md --file <folder>/scratch/business-profile.md --entity <folder>
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

## Held email

Incoming email can wait for a person to review it. `books cloud held-mail`
shows how many emails wait, the date of the oldest one, and a link to the Inbox
review view. It never shows a sender, subject or content, and no command lets an
agent read or release held email. A person reviews it in Inbox.

## Upload a document as evidence

`books cloud evidence-upload --file PATH` uploads one document to the company as
evidence. It accepts a PDF, PNG, JPEG, plain text or CSV file of 1 byte to
10 MB. The command checks the extension, the file type and the size before it
sends anything, and it compares the checksum that the service reports with the
checksum of the local file. It needs the `evidence:write` permission. An upload
does not change the books and does not match a document to a transaction. Each
upload creates a new evidence record, so do not repeat an upload that succeeded.

## Errors and retries

Cloud book requests use a 180-second network timeout by default, including
ordinary linked commands such as `books ingest`. Set `SLASHBOOKS_REMOTE_TIMEOUT_SECONDS` to a
finite positive number to change that wait for cloud requests. Explicit
`books cloud --timeout <seconds> ...` takes precedence over the environment.
Provider connector timeouts and local commands are unchanged.
This controls blocking network operations, not a total wall-clock command deadline.

```sh
SLASHBOOKS_REMOTE_TIMEOUT_SECONDS=300 books ingest <source.json> --entity <folder>
books cloud --timeout 300 retry --receipt <folder>/.slashbooks-remote-receipts/<receipt>.json
```

A longer client wait does not extend the server's work limits. The hosted
service currently allows 120 seconds for an engine command, a 150-second
internal compute request, a 60-second compute upload and a 10-second admission
wait when compute is busy. Transfer size limits also still apply. If a large
import exceeds a server limit, preserve and recover its receipt before deciding
whether to split the remaining input into smaller batches. Do not assume the
first batch failed or resubmit it with a new key.

There is no automatic chunking or batch-size flag: prepare separate normalized
JSON input files, keep each transaction's original stable `id`, and run
`books ingest` for each file after verifying the preceding result. Keep related
source records together where their import workflow requires it.

The CLI saves a receipt for each cloud write in `<folder>/.slashbooks-remote-receipts/`.
If the result of a write is uncertain, retry it with the same receipt, which
reuses the original request and idempotency key:

```sh
books cloud retry --receipt <folder>/.slashbooks-remote-receipts/<receipt>.json
```

Do not rerun the command with a new key. After a version conflict, read the
current state again and decide again.
