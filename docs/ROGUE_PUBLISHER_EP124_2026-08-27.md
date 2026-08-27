# Rogue LASSO IG publisher — identified (Blake, 2026-08-27 14:45 UTC)
Internal reference — NOT caption source material.

Verified via Pipeboard (act_547450266087104 / IG 17841425119642439).

- The 4x repeating LASSO IG captions are a PODCAST-PROMO AUTOMATION stuck on EP124,
  cycling its four promo clips (S1-S4) Wed/Thu/Fri/Sat ~10:09 ET since ~2026-03-30.
  Fired again 2026-08-27 14:09:24 UTC (reel Dci911HgDX9) and 2026-08-26 14:12:19
  (DcgZYndilOk). NOT Zernio (78 posts there, all client-gym). The duplicate-IG
  disconnect (TAP 1) did NOT stop it.
- KILL IT BEFORE the podcast library build ships (docs/PODCAST_LIBRARY_BUILD_SPEC.md)
  or two systems will post clips to the same account. Check Meta Business Suite
  Planner first (native scheduled posts are invisible to third-party tools), then
  Zapier / Make / Buffer / Later. Blake's tap.
- Duplicate burst 2026-08-27 (separate root cause, Echo-side): 'Honest numbers or no
  numbers' 3x (11:31:34, 12:06:37, 12:06:42 UTC — 5s retry pair), 'welcome Pierce
  Wellness' 2x (14:16:01, 14:37:50), five welcome posts in 15 minutes. Fix branch:
  feat/publish-guard-idempotency (idempotency keys, restart-safe claims, welcome
  pacing + once-ever dedup).
