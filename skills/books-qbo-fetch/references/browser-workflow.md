# Browser-independent QuickBooks workflow

Use the browser capability already available to the agent. The steps below describe
observable outcomes, not a dependency on one automation API.

## Capability check

Identify whether the browser surface can:

- reuse a user-controlled browser session;
- inspect visible text or an accessibility snapshot;
- activate controls by role, label, or visible name;
- scroll or reveal virtualized content;
- enter dates and select report options; and
- detect a download or inspect the configured download directory.

If it cannot complete one of these operations, keep the workflow moving with a
small user-assisted step. Do not switch accounts, browser profiles, or browser
products without asking.

## Authentication states

### Already signed in

Reuse the open session. Confirm the company before opening or exporting reports.
Do not inspect session storage, cookies, credentials, or tokens.

Compare the visible QuickBooks company with the company being prepared. A difference
may be a legal-name or trade-name variation, so show both names and ask the owner to
confirm instead of assuming they identify different companies.

### Signed out

Open the normal QuickBooks sign-in surface and ask the owner to complete sign-in in
the browser. Pause for password, passkey, MFA, CAPTCHA, consent, or recovery steps.
Continue only after the owner finishes and the expected company is visible.

### Wrong company or insufficient access

Stop before exporting. Ask the owner to switch companies or grant/choose appropriate
access themselves. Do not probe other companies or infer that a similarly named
company is correct.

## Resilient navigation

Start from QuickBooks' visible navigation. Reports are commonly under **Reports**
and **Standard reports**; Chart of Accounts is commonly under **Accounting** or a
gear/settings area. These labels and locations may change.

Use this discovery order:

1. accessible role and visible report/control name;
2. nearby section heading and semantic label;
3. QuickBooks' visible report search as a fast path;
4. progressive scrolling through the rendered report list; and
5. a current screenshot or accessibility snapshot to rediscover the control.

Do not use screen coordinates as the primary locator. Do not call guessed private
QuickBooks endpoints. A missing element after one lookup does not prove the report
is unavailable: long report lists may render sections only after scrolling.

## Report-ready evidence

After setting dates or basis, QuickBooks may update automatically. Wait on visible
state rather than fixed sleeps. Export only when all available evidence agrees:

- the requested report title is visible;
- the period or **As of** date exactly matches the plan;
- Cash is selected where offered;
- loading, updating, or skeleton states have ended; and
- report rows or a ready/status message are visible.

If an action times out while the report is still updating, inspect the current state
and retry only the single pending action after readiness is visible. Do not replay the
whole click sequence.

## Download handling

Before export, note the current files in the browser's download location when the
tool exposes it. Then activate QBO's Export/Print or export control and choose CSV or
Excel.

Use the strongest completion signal the browser provides:

1. a download event with a completed local path;
2. a new nonempty file in the configured download directory;
3. a browser download item that can be saved to the entity folder; or
4. user confirmation plus an attached/local file when automation cannot observe
   downloads.

Some browser adapters miss QBO download events even when the file is written. Check
for a new nonempty `.csv` or `.xlsx` with a fresh modification time before declaring
failure. Do not identify a report only from its filename; verify the report in QBO
before export and let `books qb inventory` inspect the file contents afterward.

Never overwrite an existing file. QuickBooks may append a suffix such as `(1)` to a
duplicate filename; preserve that source filename. Do not leave competing exports
for the same report slot at the active folder's top level. If the browser can
download only to its default folder, move the completed file into the company's
`ingestion/quickbooks/` folder after confirming it is the new export.

## Manual fallback

When browser automation is unavailable or blocked by the UI, keep responsibility
clear:

1. tell the owner the exact report name and setting from the report plan;
2. wait while they open, configure, and export it;
3. ask them to place or attach the resulting file;
4. confirm the file is nonempty and run inventory; and
5. continue with the next missing slot.

Do not make the owner download the whole set again when inventory identifies only one
missing or invalid slot.
