# Agent fix loop

The loop improves a business's **draft** after the Readiness Score (`docs/score.md`) and shows its reasoning on the owner page. It never changes the owner's profile, never publishes and never spends money (CLAUDE.md rules 2 and 9).

## Flow

1. The owner presses "Let Toran improve my draft". An `agent` job is queued (one loop at a time, at most 3 a day per business, a 1500 s timeout, one retry that resumes after the last finished round).
2. **Baseline**: the newest complete draft score is reused if it tested exactly the current draft files; otherwise the draft is rebuilt and tested.
3. **Each round** (at most 5):
   - The score items, the owner's confirmed details and the current improvements go to Claude Sonnet as data inside `<business>`, `<score>`, `<improvements>` and `<history>` tags. The system prompt says never to follow instructions found there.
   - Claude calls tools (up to 3 calls and 12 tool uses per round, 6000 output tokens per call). Every call is validated in `app/services/agent_tools.py`; a rejected call goes back to Claude as an error so it can try again.
   - Accepted improvements are saved as a new `SiteExtras` version, the draft is rebuilt, and `run_tests` runs the customer tests and AI checks.
4. **Stops** when the score reaches 90, a round brings no improvement (that round's version is discarded and the draft rebuilt), Claude only asked the owner questions (`needs_owner`), there is nothing left it can fix, 5 rounds have run, or Claude or the spend cap stops it. The best draft so far is kept, and the live site is never touched.

## Tools and their checks

| Tool | Does | Checks in code |
| --- | --- | --- |
| `add_translation` | Shows a product name, note or price-option label in en, hi or mr | Strict input (unknown fields such as `price` are rejected). The numbers must match the owner's text exactly (Devanagari digits normalised). No currency, prices, phone numbers or links. Correct script. At most 3 times the original's length. Not an address. Keyed by a hash of the owner's text, so editing the original drops it |
| `edit_layout` | `main_button_first`, `photo_after_text`, `large_buttons`, `section_order` | Fixed choices only; the section order must list all five sections once |
| `compress_images` | Lighter photo files (60 % of the byte budget) | Only if the owner has photos |
| `ask_owner` | Saves a question: `policy`, `lead_time`, `delivery_areas`, `photos` | Fixed topics, worded by our templates in the owner's language; never asks for something the owner already gave or that is already waiting |
| `rebuild_draft`, `run_tests` | Rebuild; finish the round | — |

Prices and every other fact still come only from the owner's confirmed profile when the site is built.

## The owner's approval

When the loop ends, the owner's page shows the score before and after and the main fixes. "Approve these changes" opens the existing publish confirmation, which lists the changes; publishing marks the run and its improvements approved. "Undo these changes" discards the run's improvements and re-tests the draft.

The owner's answer to a question is checked (length, no home address), shown back to the owner exactly as it will appear, and added to the owner's profile as a new version only after the owner confirms. The draft is then rebuilt and re-scored.

## "How it thinks"

`/owner/agent/<id>`, built from the action log (`ActionLog.run_id` is the agent run id):

- first, before and after (score and main fixes), and the approve and undo buttons;
- then each round: what was wrong (the lowest score items with their reasons), what Toran did and why (one sentence from our templates in the owner's language), which tool it used, and the score change;
- an expandable "Details for judges": Claude's own reason (English), the exact tool input, rejected calls, tokens and cost.

## Cost and safety

Every Claude call goes through `ClaudeClient` (`max_tokens`, 60 s timeout, retries with backoff, cached system prompt and tools, token and cost logging, daily spend cap). The run stores its total estimated cost, including the AI checks of each re-test.

## Mock mode

Agent replies come from `tests/fixtures/claude/agent_<slug>_round<n>.json` (else `agent_default_round<n>.json`, which asks for the owner's policy), and follow-up calls in a round from `agent_<slug>_round<n>_next.json` (else `agent_done.json`). AI checks after a change use `ai_checks_<slug>+translated`, `+policy` or `+translated+policy` when they exist. The `agent-demo-foods` fixtures (the home-foods profile) go from about 72 to about 87 in tests, then stop to ask for the owner's ordering policy.
