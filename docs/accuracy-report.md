# Accuracy report

`books backtest run --truth` writes a report that anyone can re-run. It scores
the categories that Slashbooks chose against a ground-truth file, and it keeps
the row count and the coverage split next to every percentage.

## Run it

```sh
books backtest run --entity /path/to/company \
  --qb-folder /path/to/qb-exports --from 2026-01-01 --to 2026-03-31 \
  --banksync-json /path/to/bank.json \
  --truth /path/to/truth.csv [--report DIR] [--seed N] [--json]
```

- `--truth PATH` is a CSV with the columns `source_id,account`. `source_id` is the
  bank transaction id. `account` is the correct category account. Extra columns are
  ignored. A repeated `source_id` is an error.
- `--report DIR` is the folder for `accuracy-report.json` and `accuracy-report.md`.
  The default is `reports/migration-confidence/` in the company folder.
- `--seed N` records the seed of synthetic data in the report. It changes nothing else.
- `--json` prints the report on stdout. Progress lines go to stderr.
- `--report`, `--seed` and `--json` need `--truth`.

Only the rules in `category_rules` in `entity.json` post a transaction during a
backtest. A transaction without a matching rule waits in the review list. It has
no category yet, so the report counts it as pending and does not score it.

## How a row is scored

Each truth row is joined on `source_id` to the effective ledger postings. A
correction chain resolves to its latest entry. The category is the posting that
is not on a bank or card account. Each row gets one status:

| Status | Meaning |
|---|---|
| `agree` | Posted, and the category equals the truth. |
| `disagree` | Posted, and the category differs from the truth. |
| `pending` | Waiting in the review list. No category yet. |
| `missing` | Not posted in the period and not waiting. |
| `not_comparable` | Posted with a split or with no single category posting. |

`category_agreement.rate` is `agree` over `compared` (`agree` plus `disagree`).
`end_to_end_rate` is `agree` over every truth row, so pending and missing rows
count as not right. The report always shows both with their n.

## JSON schema (`schema_version` 1)

| Field | Content |
|---|---|
| `schema_version` | `1`. A field changes or goes away only with a new version. |
| `report` | `slashbooks-accuracy-report`. |
| `generated_at` | UTC time. The only field that differs between two runs. |
| `engine` | `version`, `git_commit` and `git_dirty` (`null` when not a checkout). |
| `seed` | The `--seed` value or `null`. |
| `period` | `from`, `to`. |
| `truth` | `file` (name only), `sha256`, `rows`. |
| `thresholds` | `material_abs`, `material_pct`, `small_sample_n`. |
| `n` | Number of truth rows. |
| `headline` | One sentence with the rate, n and the coverage split. |
| `category_agreement` | `compared`, `agree`, `disagree`, `rate`, `end_to_end_rate`, `per_account`, `confusion`, `disagreements`. |
| `coverage` | `truth_rows`, `auto_posted`, `compared`, `pending`, `missing`, `not_comparable`, `compared_rate`. |
| `pnl_line_variance` | `lines_compared`, `lines_matched`, `lines_material`, and `lines` with `account`, `ours`, `reference`, `delta`, `material`. |
| `matched_count` | Transactions matched to QuickBooks by date, amount and description. |
| `transaction_matching` | `matched_count`, `unmatched_ours`, `unmatched_qb`. |
| `comparison_errors` | Errors from the QuickBooks comparison. |
| `notes` | Warnings, such as a small sample. |
| `does_not_measure` | What the run does not measure. Print it with the result. |
| `per_transaction` | `source_id`, `truth`, `ours`, `status` for each truth row. |

The same data, rules and engine commit give the same numbers. Compare `truth.sha256`,
`engine.git_commit` and `seed` to check that two reports come from the same inputs.

## Synthetic data

`tests/fixtures/synthetic/generate.py` makes a fictional company from a seed:
bank rows, a truth file, a matching QuickBooks export, and `entity.json` rules that
cover some vendors and get one vendor wrong on purpose. It also writes a manifest
with the expected counts, so a test can check the engine against known answers.

```sh
python3 tests/fixtures/synthetic/generate.py --seed 1 --rows 120 --out /tmp/synthetic
```

## What the report does not measure

The report lists this in its own `does_not_measure` block. Quote that block
whenever you quote the rate. In short: only the truth rows are scored, the truth
file is not audited, pending rows are not scored, agent judgment in the review
queue is not measured, the report cannot detect rules that were written after
reading the truth file, agreement counts transactions and not amounts, and the
QuickBooks transaction match is a heuristic.
