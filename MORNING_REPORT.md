# Echo — Morning Report

Date: 2026-07-01
Branch: `main` · Repo: `lassoframework/lasso-echo`
Publishing: **OFF** (draft-only) — `AGENT_ENABLED` and `AGENT_PUBLISH_ENABLED` both default `false`.

## What shipped

- **Growth pack** (commits `dde2f3a`, `c37c64e`):
  - **CTA rotation** on every draft (approved CTAs pulled from the brand-voice doc,
    never invented).
  - **Hashtag cap at 5** (`drafter._select_hashtags`, `HASHTAG_LIMIT = 5`), selecting
    only from the approved set in the voice doc.
- **Carousel support** — multi-slide creatives load from a subfolder of 2+ images
  (`library._load_carousel`, `media_type="carousel"`), draft the same way as singles,
  and have an Instagram multi-child carousel publish path
  (`meta_publisher._publish_instagram_carousel`) that stays **behind the draft-only
  guard** (dormant until publishing is armed).
- **Creative + trackers:** speed-to-lead creative tracked (`content_library/speed_to_lead.jpg`),
  a 3-slide test carousel folder, 14 branded cards with `.json` sidecars, and the
  2026 brand-bible edits. `PROGRESS.md` updated (tests 31/31, growth-pack items marked done).

## Tests

**31 of 31 passing** (`python -m pytest -q`, run in `.venv`). No failures.

## What did NOT ship

- **Reels support** — not built. No `reel` path exists in the code.
- **Stories support** — not built. Only a documented stub
  (`agent/stubs.py:post_story` → `NotImplementedYet`), intentionally deferred.
- **HTML tracker** (`echo_build_tracker.html`) — left for Blake to refresh by hand
  (only `PROGRESS.md` was updated in this loop).

## By-hand items left for Blake

- Rename the Slack app to **Echo** and wire `#echoclaude` (bot token + channel id).
- **Host the creative media** so Instagram has public URLs (IG needs a public
  `image_url`/`video_url`; local files must be hosted first).
- Approve the **brand-voice bible** (`brand_voice/lasso_voice.md`).
- Meta App + Graph permissions approval; set **per-account tokens** by hand in Railway.
- New Railway project `lasso-echo` + `echo` service + env vars.
- **Arm publishing** (`AGENT_PUBLISH_ENABLED=true`) — only once drafts look right.
- Refresh the HTML tracker to match `PROGRESS.md`.

## Notes

- No secret was printed, logged, or committed. Tokens are read lazily from env in
  `accounts.py`, never stored on an object; `config.py` holds only env-var *names*.
- Publishing remains **OFF**; nothing was deployed and no live Meta/Slack call was made.
