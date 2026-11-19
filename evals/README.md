# Evals

`make eval` reruns extraction on every case whose answer key is marked `"reviewed": true` and
checks the answers in code. In mock mode (`CLAUDE_MOCK=1`) it uses saved answers from
`tests/fixtures/claude/extract_eval_<id>.json`, which tests the scoring itself; with
`CLAUDE_MOCK=0` it calls Claude.

- `cases/` holds fake sample businesses only. It is committed.
- `private/` holds real cases (voice notes, transcripts, answer keys). It is git-ignored and
  blocked by a pre-commit hook. `make eval` includes it when it exists.
- `reports/` holds saved reports. It is git-ignored because reports can name private cases.

## Case format

```json
{
  "id": "bakery-asha",
  "reviewed": true,
  "language": "mr",
  "input": {"type": "transcript", "transcript": "..."},
  "expected": {
    "business_type": "bakery",
    "phone": "9000012345",
    "area": "Kothrud, Pune",
    "delivery_areas": ["Kothrud"],
    "lead_time_numbers": ["2"],
    "products": [{"name": "Chocolate cake", "options": [{"label": "1 kg", "price": 850}]}],
    "questions": ["phone"]
  }
}
```

When the same items can fairly be grouped in more than one way, use `"products_any_of"` instead
of `"products"`: a list of alternative product lists. Each alternative is checked with the same
strict name and price checks, and the best-scoring one counts, so every price must still match.

`input.type` is `transcript`, `photo` (with `file`, relative to the case) or `audio` (with
`file` and the reference `transcript`). Audio goes through speech-to-text only when
`STT_PROVIDER=sarvam`; otherwise the reference transcript is used.

## What is checked

- Prices, the phone number and lead-time numbers must match exactly.
- Product names are matched after normalising spacing and case. Names that still differ are
  judged by Claude (Haiku), and every decision is listed in the report for you to check.
- Areas are compared as sets; expected questions must appear in `missing`.
- Each failed check names the likely cause: speech-to-text if the transcript already lacked the
  value, otherwise Claude.

## Updating the home page figure (evals/latest.json)

The home page's "Facts and test results" group shows the totals of the latest real eval run from
`evals/latest.json`. That file is committed and holds totals only, never case names. Update it by
hand after every real run (`make eval` with `CLAUDE_MOCK=0`); never copy numbers from a mock run.

1. Run `make eval`. The report is saved as `evals/reports/eval-YYYYMMDD-HHMMSS.md` and its
   first line must read `# Eval report (real Claude)`.
2. Copy the totals into `evals/latest.json`:

   | Field | From the report |
   | --- | --- |
   | `checks_passed` | the first number in `Checks passed: 80 of 80` |
   | `checks_total` | the second number in that line |
   | `cases` | the number in `Cases scored: 11` (skipped cases do not count) |
   | `prices_exact` | `true` only if `Prices exact: 100%`, otherwise `false` |
   | `run_date` | the date in the report's file name, as `YYYY-MM-DD` |

3. Check it: `.venv/bin/python -c "from app.services.showcase import load_eval_summary as l; print(l())"`
   prints the values, or `None` if the file is missing or not valid (the figure is then hidden).
4. Commit `evals/latest.json` with the change that prompted the run.
