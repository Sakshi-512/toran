# Customer Readiness Score

Five parts of 20 points each, 100 in total. The limits are starting values. Adjust them after the first real tests show what sites score. If a check does not apply to a business, its points are shared among the other checks in that part.

| Part | Check | Points | How it is checked |
| --- | --- | --- | --- |
| Speed on slow internet | Main content appears within 4 s for full points, 7 s for half, 10 s for a quarter | 14 | Measured |
| Speed on slow internet | Page weight under 500 KB for full points, under 1 MB for half | 6 | Measured |
| Phone and laptop | Order button visible without scrolling at 360 px | 8 | Measured |
| Phone and laptop | All buttons at least 44 x 44 px | 4 | Measured |
| Phone and laptop | No sideways scrolling at 360 px | 4 | Measured |
| Phone and laptop | Nothing overflows at 1280 px | 4 | Measured |
| Language | Price, delivery, contact and how to order are clear in each site language, 5 points each | 20 | AI-assessed |
| Trust | Phone number visible | 5 | Measured |
| Trust | Delivery area shown, never a home address | 4 | Measured |
| Trust | Real product photos | 4 | AI-assessed |
| Trust | Ordering or refund policy shown | 4 | AI-assessed |
| Trust | Secure connection (HTTPS) | 3 | Measured |
| Customer questions | Share of the business checklist answered, times 20 | 20 | AI-assessed |

## Rules for the score

- Every item records its points earned, a plain-language reason, and whether it is `measured` or `ai_assessed`.
- Items marked `ai_assessed` show as "pending" until the AI check runs.
- Running the same site twice should give nearly the same result. Measured items use the median of three runs.
- The total is the sum of earned points, rounded to a whole number.

## How the AI-assessed items are scored

- One Claude call (claude-sonnet-5-5, with the phone screenshot) per test run judges the visible text of the English, Hindi and Marathi pages against the business type's checklist in `docs/checklists.md`. Its answer is JSON, validated before use.
- AI checks run **once per test run, not three times** like the measured items. A second and third call would triple the cost for little gain. To keep answers repeatable, the answer must follow a fixed JSON schema, and points come from fixed verdicts (yes = full, partly = half, no = none) rather than from wording. The model does not accept a temperature setting.
- Language: 5 points each for price, delivery, contact and how to order. Each topic's points are the average of its verdicts on the three language pages.
- Real product photos: yes, partly or no. Sample businesses use stock photos, so this item is "not applicable" for them and its points are shared within Trust.
- Ordering or refund policy: yes, partly or no. A site with no policy text scores no.
- Customer questions: the share of checklist questions answered, times 20.
- If the AI check fails, the daily spend cap is reached, Claude declines, or the answer cannot be read, these items stay "pending" with a friendly reason. The measured items are still saved.
