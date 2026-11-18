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
