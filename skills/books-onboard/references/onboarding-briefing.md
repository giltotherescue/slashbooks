# Onboarding briefing: schema and worked example

`briefing.json` is the output of prepare mode in [SKILL.md](../SKILL.md). The cloud
server validates it when the operator uploads it. A live agent reads it during a
short onboarding meeting with the owner. The briefing is what the accounts show.
The agenda is the list of things only the owner can tell us. The agenda is the
floor, not the ceiling: the live agent may reorder, merge, skip or add points.

## Schema (version 1)

```json
{ "schema_version": 1,
  "books_revision": 412,
  "prepared_by": "hand",
  "briefing": {
    "opening": "...",
    "period": { "first": "2026-01-02", "last": "2026-10-14" },
    "accounts": [ { "name": "Mercury checking", "source": "banksync", "transactions": 612 } ],
    "patterns": [ { "text": "...", "counterparty_keys": ["adobe"] } ],
    "odd_vendors": [ { "key": "atlas contractors", "why": "..." } ],
    "transfers": [ { "from": "A", "to": "B", "count": 9, "total": "14200.00", "note": "..." } ],
    "top_vendors": [ { "key": "adobe", "total_out": "659.88", "count": 12 } ],
    "documents": { "count": 14, "missing": ["February Mercury statement"] },
    "known_profile": [ "Business name and website are saved." ]
  },
  "agenda": [ { "id": "p1", "order": 1, "group": "basics",
    "title": "...", "reason": "...", "priority": "must",
    "counterparty_keys": [], "may_settle_with": ["p2"] } ] }
```

Field rules:

| Field | Rule |
|---|---|
| `schema_version` | Always `1`. |
| `books_revision` | The `books_revision` of the summaries you read. |
| `prepared_by` | `hand` for a prepare run started by a person, `automation` later. |
| `briefing.opening` | Up to 2,400 characters. States what was read, roughly how many transactions, how many points and about how many minutes. |
| `accounts[].source` | `banksync`, `statement`, `stripe` or `quickbooks`. |
| `money` fields | Decimal strings with at most two decimals, for example `"14200.00"`. Copy them from the summaries. Never add them yourself. |
| `counterparty_keys`, `odd_vendors[].key`, `top_vendors[].key` | Must be a key in `counterparties[]` of the summaries for the same `books_revision`. Upload rejects any other key. |
| `agenda` | 1 to 25 points. Unique `id` (letters, digits, `_`, `-`). |
| `agenda[].order` | Whole numbers that increase down the list. |
| `agenda[].group` | `basics`, `money_in`, `owner_pay`, `transfers`, `vendors`, `documents`, in that order. A later point never has an earlier group. |
| `agenda[].reason` | One line, at most 200 characters, in a bookkeeper's voice. |
| `agenda[].priority` | `must` only when the answer blocks correct books. Otherwise `should`. |
| `agenda[].may_settle_with` | Ids of other points. Not its own id. |
| Whole file | At most 64 KiB. Every text field is screened. A flagged field holds the briefing until a person releases it. |

## What belongs where

- `patterns`: what the data shows in broad strokes. These are statements, not questions.
- `odd_vendors`: payees that need the owner. Give the observable reason ("Three payments
  over $600 with no invoice on file"), never a guess about what they are.
- `transfers`: movements between the company's own accounts, with the note "Looks like ..."
  when the summaries say `transfer_like`.
- `known_profile`: what the saved profile already tells us, so the live agent does not ask.
- Agenda points: only what the data cannot answer.

## Do not ask what the data shows

Not agenda points:

- "Which bank do you use?" The integrations show it.
- "Do you pay for Adobe?" The summaries show twelve monthly payments.
- "Are these card payments?" Ask only if the transfer pattern is unclear.

Agenda points:

- "What does the business do, and how does it get paid?" The data cannot say.
- "Atlas Contractors: contractors?" The data shows amounts and dates, not the relationship.

## Reasons a bookkeeper would give

Good:

- "Three payments are over $600, and I need to know if they are contractors, because that decides the 1099."
- "Regular transfers go to your personal account, and I need to know if they are pay or loans."

Not good (a form field, a vague reason, or advice):

- "Vendor classification" (no reason)
- "To improve accuracy" (not a bookkeeper's reason)
- "You can probably deduct this" (advice; never write it)

## Worked example

A design studio with a Mercury checking account and a Mercury card, 850
transactions from 2026-01-02 to 2026-10-14. The full file is
`hosted/qa/onboarding/briefing.example.json` in the cloud repository.

Opening:

> I went through your Mercury checking account and your Mercury card. There are about
> 850 transactions from January 2 to October 14, mostly software, a few contractors,
> and card payments between your own accounts. I have 12 things to ask, and it should
> take about ten minutes. I will tell you why I am asking each one.

Agenda (12 points, 4 `must`):

| Id | Group | Priority | Title | Reason |
|---|---|---|---|---|
| p1 | basics | must | What the studio does | I need this to read the deposits and the software costs correctly. |
| p2 | basics | must | How clients pay you | Deposits come by transfer and by Stripe, and I need to know which is which. |
| p3 | money_in | should | Brightline Studio and Kestrel Labs deposits | These two are most of your income, so I want to know if they are retainers or projects. |
| p4 | money_in | should | Stripe payouts | Stripe pays out net of fees, so I need to know what the sales are for. |
| p5 | owner_pay | must | How you pay yourself | Regular transfers go to your personal account, and I need to know if they are pay or loans. |
| p6 | owner_pay | should | Personal costs on the business card | A few card purchases look personal, and I want to keep them out of business costs. |
| p7 | transfers | should | Payments from checking to the card | Nine payments look like card payments, and I need to be sure they are your own accounts. |
| p8 | vendors | must | Atlas Contractors | Three payments are over $600, and I need to know if they are contractors, because that decides the 1099. |
| p9 | vendors | should | Northwind Print | A one-time $2,400 payment could be a client project cost or an asset. |
| p10 | vendors | should | Amazon and Venmo | Small payments with no clear payee are the ones I cannot sort without you. |
| p11 | documents | should | W-9 and invoices for Atlas and Northwind | I have no paperwork for these payees, and a W-9 is easiest to get now. |
| p12 | documents | should | The February Mercury statement | It is the one statement missing, and it closes the gap in the checking history. |

One point shown in full:

```json
{ "id": "p8", "order": 8, "group": "vendors", "title": "Atlas Contractors",
  "reason": "Three payments are over $600, and I need to know if they are contractors, because that decides the 1099.",
  "priority": "must", "counterparty_keys": ["atlas contractors"], "may_settle_with": ["p11"] }
```

Notes on the example:

- The bank connection, the Adobe and Figma subscriptions and the likely card payments
  are in `briefing` (data the live agent can quote) and have no agenda point of their own.
- p2 can settle p3 and p4, and p5 can settle p6. `may_settle_with` lets the live agent
  skip a point that an earlier answer already covers.
- Only p1, p2, p5 and p8 are `must`. If the owner leaves early, those become follow-up questions.
