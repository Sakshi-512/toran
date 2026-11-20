# How Toran uses Claude and GoDaddy (for the project brief)

120 words, built only from what the code does. The full table with file paths and tests is in the
README ("How Toran uses Claude and GoDaddy").

Toran uses Claude where judgement is needed and code everywhere else. Sarvam Saaras v4 turns Marathi, Hindi or English voice notes into text; Claude Sonnet reads the transcript or a price-list photo and returns a validated business profile the owner confirms. A real Chromium browser tests the site: nine measured checks (52 points) plus four Sonnet-judged checks (48 points) form the Readiness Score. A Sonnet agent loop improves a draft with six guarded tools, at most five rounds, rolling back rounds that do not help; it cannot change facts, publish or spend. Claude Haiku suggests web addresses, and the gddy CLI checks live GoDaddy prices. It runs on a GoDaddy VPS with Caddy HTTPS, wildcard DNS and a GoDaddy mailbox.
