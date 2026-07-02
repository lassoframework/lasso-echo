# Social proof source (EXAMPLE, synthetic - copy to social_proof.md and fill with real entries)

Echo renders a social proof card from this file at most ONCE per account per week
(the proof weekday, default Wednesday, env AGENT_SOCIAL_PROOF_DAY). Rules:

- Every entry needs `Permission: yes` AND `Verified: YYYY-MM-DD` or it is SKIPPED
  with a Slack notice and never rendered. Permission means the person named in the
  attribution approved public use. Verified means YOU confirmed the quote or number
  is true on that date, with a source you could show.
- A missing or empty file simply turns the feature off; normal drafting continues.
- Per-account convention: `social_proof.<account_key>.md` beside this file wins
  over the shared `social_proof.md` for that account.
- Quote entries render as a QUOTE CARD (quote large, attribution small). Stat
  entries render as a NUMBER CARD (stat huge, one support line, attribution small).
- No dashes in entry text (brand copy mechanics); use "to" for ranges.

## Entry
Quote: Your coaches actually check in on me. I never had that at my old gym.
Attribution: Sarah M., member since 2024
Permission: yes
Verified: 2026-06-28

## Entry
Stat: 18 pounds down in 12 weeks
Support: Mike's first strength block
Attribution: Mike R.
Permission: yes
Verified: 2026-06-30

## Entry
Quote: This one is missing a verified date, so Echo will SKIP it with a notice.
Attribution: Example person
Permission: yes
