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

---

## Update — Reels shipped (2026-07-01)

- **Reels support (draft-only)** shipped, mirroring the carousel pattern:
  - `meta_publisher._is_video()` routes a video creative (`.mp4`/`.mov`) to a new
    `_publish_instagram_reel()` — REELS container (`video_url`, `share_to_feed=true`),
    bounded polling of the container `status_code` until `FINISHED`, then publish.
  - This path sits **behind the unchanged draft-only short-circuit** in `publish()`;
    it is dormant until publishing is armed. The caption still comes only from the
    client note (missing note still blocks) — no fabrication.
  - The Slack approval card now labels a video creative **"Reel — <filename>"**
    (carousels keep their "Carousel — N slides" label).
- **Tests: 35 of 35 passing** (was 31; +4 for Reels: video detection, note-caption
  draft, draft-only would_publish, Slack Reel label).
- **Still NOT shipped:** Stories (documented stub only).
- **Publishing still OFF**; no secret was printed, logged, or committed; tests use
  fake clients only (no live calls).
- By-hand items for Blake are unchanged from above (Slack app rename + wiring, host
  media, approve bible, tokens/Meta permissions, Railway, then arm publishing).
