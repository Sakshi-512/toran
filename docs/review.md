# Independent code review (November 2026)

A strict review of the whole codebase against CLAUDE.md, looking for security holes, bugs and
race conditions, rule violations, privacy problems, messy code, missing tests and anything that
would break on a fresh server. It found 30 items (4 critical, 13 important, 13 minor). The
fixes came in two batches. Each fix has a regression test in `tests/test_review_fixes.py`. For
items 1 to 7 the test was written first and seen to fail before the fix.

## Fixed

| # | Level | Finding | Fix |
| --- | --- | --- | --- |
| 1 | Critical | The sample voice notes were read from `tests/`, which the Docker image leaves out, so "try a sample" failed in production | Moved to `app/samples/stt/`; a test checks that the files are in the image |
| 2 | Critical | Business ids were reused after a delete, so an old session could open a new business | `AUTOINCREMENT` ids (migration 0013) and a per-business session epoch checked on every request |
| 3 | Critical | The agent loop called Claude on the event loop, freezing every request while it waited | Claude decisions run in a worker thread |
| 4 | Critical | Upload size was checked only after the whole body was read | 70 MiB limit in Caddy and in an app middleware (declared or streamed), at most 12 files per form |
| 5 | Important | Publishing during an agent run could make unseen changes live (rule 2) | Publish is refused while the agent works, and if the draft changed since the owner saw it |
| 6 | Important | A crashed or timed-out agent run or domain lookup blocked the owner for good | Runs always end with a reason; stale runs and lookups are closed |
| 7 | Important | Each job worker requeued every running job at startup, so jobs could run twice | Stale jobs are requeued once, before any worker starts |
| 8 | Important | Deleting during a running job left files that a new business could inherit | Two-step delete: everything stops at once; the wipe waits until no job is running, and the web address stays taken until then |
| 9 | Important | A double tap on Confirm could crash or create a second business | The intake is claimed in one statement; a taken address sends the owner back to the cards |
| 10 | Important | No init process or shared memory for Chromium; root-owned backup folder; backups only by hand | `init: true`, `shm_size: 1gb`, the app makes the daily backup, deploy step for the folder |
| 11 | Important | The gddy CLI was not in the image, and GoDaddy mocking was tied to `CLAUDE_MOCK` | Pinned gddy 0.2.37 with SHA-256 checks for both Linux builds, `/health` reports it, and `GDDY_MOCK` is its own switch |
| 12 | Important | A phone number change (where orders go) needed no PIN | The PIN is needed whenever the phone number changes |
| 13 | Important | Unconfirmed intakes and kept inputs had no time limit | Deleted after 7 and 30 days; the privacy page states this, and test screenshots, in all three languages |
| 14 | Important | Anonymous users could use the whole Claude budget; speech-to-text had no cap | 30 intakes a day per address, `STT_DAILY_MINUTES_CAP`, and 30% of the Claude cap kept for existing businesses |
| 15 | Important | Owner text could close a prompt's data tags early | Angle brackets in owner, customer and page text are neutralised before every prompt; eval case `injection-attempt` |
| 16 | Important | Only one agent run at a time for the whole app | Two at once; queued runs show their place in line |
| 17 | Important | Tests were missing for the risky paths above | Added with each fix. Cross-business access was already tested for photos, enquiries and the agent panel; score reports and owner questions still have no such test |
| 19 | Minor | Names such as `autodiscover`, `login` or the app's own name could become a business address | Added to the reserved names |
| 20 | Minor | Two publishes at once crashed, and a failed save could still change the live site | Lock per business, unique version numbers (migration 0014), live folder replaced only after the save |
| 21 | Minor | A bad number in the confirmation form caused a 500 page | Card counts are read safely |
| 24 | Minor | Alert emails could include email addresses, and changing values bypassed the hourly limit | Emails redacted in logs (`n***@domain`); alerts grouped by message template |

## Deferred on purpose

| # | Finding | Why it waits |
| --- | --- | --- |
| 18 | The PIN lockout never gets stricter | Both the secret link and the PIN are needed, and the link is the strong secret; an increasing lockout comes with the next auth change |
| 22 | The style preview can be out of date after agent changes | Only the owner's private preview is affected, never the draft or the live site; it fixes itself on the next profile or photo change |
| 23 | Logout cannot revoke a copied session cookie | The session epoch from item 2 now ends every session on delete or new access; a per-session revoke list is not worth the cost yet |
| 25 | Chromium runs without its sandbox | It only opens our own generated sites inside the non-root container; documented in `docs/deploy.md` |
| 26 | Duplicated helpers (get business or 404, latest profile, timezone fixes, IST) | Code quality only; to be merged in one refactor, not mixed with security fixes |
| 27 | Layering (route-to-route imports, function-level imports, logic in the confirm route) | Same refactor as 26 |
| 28 | `deleted_at` was checked but never set | Now used by the two-step delete (item 8); the remaining scattered checks go with the refactor |
| 29 | Mixed session handling in routes and services | Same refactor as 26 |
| 30 | Restoring a backup brings back data deleted within the last 7 days | Inherent to backups; the restore steps in `docs/deploy.md` say what to do after a restore |
| 31 | Devanagari phone digits are treated differently on two pages: the confirm page turns `०-९` into `0-9` before checking (`draft_from_form` uses `ascii_digits`), My details does not (`details.check` calls `normalise_phone` on the raw text). So `९८७६५४३२१०` is accepted on confirm and refused on My details, and a number mixing scripts can be stored with Devanagari digits from My details | A server validation change, outside the feature freeze. The live checks (`app/static/validate.js`) mirror each page exactly as it is today; fixing it means calling `ascii_digits` in `details.check` and `details.save`, with a test |

## Still to do before submission

- TODO: the README headline says "Real-voice test: results in the project brief". Fill in the
  final tally of the real-voice test (field and price accuracy, number of real voice notes) in the
  brief, and then in the README headline.
- TODO: the "Real-world results" table in the README is filled in, except the price row's
  denominator (how many confirmed prices the first guess was compared with). `make server-accuracy`
  prints it as "prices X/Y".
- TODO: agent results. Each agent run is recorded in the `agentrun` table (`AgentRun` in
  `app/models.py`): `score_before`, `score_after`, `cost_usd_estimate`, `rounds`, `stop_reason`
  and `status`; each round's kept or rolled-back result is in the action log. Once the production
  results are in, the README can state them per real run instead of the general sentence.

## What could not be checked here

- The x86_64 gddy build was checked against the release checksums, but only the arm64 build was
  installed and run (the review machine is arm64).
- The new limits and Docker settings were tested locally, not on the GoDaddy VPS.
