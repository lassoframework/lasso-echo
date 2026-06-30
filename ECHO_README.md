# Echo — LASSO Social Agent (Stage 1)

Echo is the LASSO social media agent. New repo, own body. It reuses the Ranger
*spine* as a COPIED PATTERN only: guardrails, approvals, Slack control surface,
flags default OFF, per-account trust ladder. It does **not** branch off Ranger or
openclaw and does **not** share Ranger's running process.

## Deploy + separation (clean from commit one)

- **Repo:** `lasso-echo` (its own repo).
- **Railway:** its own service `echo` in a **new Railway project `lasso-echo`**,
  separate from Ranger, so Echo's spend is its own clean line item (it becomes the
  $99 product). Own deploy, own URL, own env vars.
- **Shared brain, separate body:** Echo reads the same LASSO brain vault as Ranger
  but **read-only, in its own process**. Same DNA, separate organism. A crash or
  bad token in Echo can never reach Ranger.
- **Slack:** approval cards go to **`#echoclaude`**, separate from Ranger's
  `#rangerclaude`.
- **Tokens:** Echo's Meta tokens are set by Blake's own hand in Echo's Railway
  service env. Different from Ranger's ad tokens. Never in code, chat, or logs.

**Stage 1 = prove it on ourselves.** The agent drafts one social post per day per
LASSO account, holds every post for your approval in Slack, and on approval
publishes to Meta. It ships **draft-only**: publishing is OFF until you arm it by
hand.

---

## The flow

1. **Daily draft.** Once a day, for each connected account, the agent drafts ONE
   feed post: caption + hashtags + a creative it selects from the local content
   library. One post, per account, per day.
2. **Approval card to Slack.** It posts a card to your channel: account, scheduled
   time, the creative reference, the caption, the hashtags. Three actions:
   **Approve / Edit / Skip** (buttons, plus a reply protocol).
3. **Hard gate.** Nothing publishes without your approval. First post to any
   audience is never automated. No exceptions in Stage 1.
4. **On action:**
   - **Approve** → publishes to the right Meta surface (IG feed / FB Page).
     In draft-only mode this only logs `would publish` — no real write.
   - **Edit** → your note revises the draft; it re-posts for approval.
   - **Skip** → the draft is dropped.
5. **Log.** Every published (or would-publish) post is logged — account, time,
   caption, media id, mode — for later reporting.

Only **U06EPUUCL13** (Blake) can approve. Anyone else is denied and ignored.

---

## Flags (both default OFF)

| Flag | Env var | Default | Meaning |
|------|---------|---------|---------|
| Master | `AGENT_ENABLED` | `false` | If OFF, the agent does nothing. |
| Publish | `AGENT_PUBLISH_ENABLED` | `false` | If OFF, **draft-only**: Approve logs `would publish`, never writes to Meta. |

**Ship state: both OFF.** Run with master ON / publish OFF to watch drafts for
days. Arm `AGENT_PUBLISH_ENABLED=true` by hand only when the drafts look right.

The draft-only guard is enforced in two places (approval flow *and* the publisher
itself), so a real Meta write cannot happen by accident.

---

## Run it

```bash
pip install -r requirements.txt

python -m agent status        # show flags + gate state
python -m agent dry-run       # run the whole loop OFFLINE, no tokens (watch a draft)
python -m agent run-daily     # draft one post per account, post cards to Slack
python -m agent listen        # always-on: Slack approvals + daily scheduler (Socket Mode)
```

## Deploy runbook (Railway + Slack + Meta)

### Railway

1. New project `lasso-echo`. In the create menu, pick **GitHub Repository** and
   select `lassoframework/lasso-echo`. (Not a database, not a template.)
2. Railway autodetects Python via Nixpacks. Set the service start command to:
   `python -m agent listen`  (one always-on service = approvals + daily drafts).
3. Add a **Volume** mounted at `/data` so runtime memory survives redeploys, then
   set `AGENT_PENDING_PATH=/data/pending_drafts.json` and
   `AGENT_POST_LOG_PATH=/data/post_log.jsonl`. Without a volume, Railway's
   filesystem is ephemeral and pending drafts reset on each deploy.
4. Set all env vars by hand (see `.env.example`). Leave `AGENT_PUBLISH_ENABLED`
   OFF. Set `AGENT_ENABLED=true` when you want it live in draft-only mode.
5. (Optional, more robust than the in-process scheduler) add a second service or
   Railway cron running `python -m agent run-daily` daily, and set
   `AGENT_SCHEDULER_ENABLED=false` on the listener service.

### Slack

1. Create the app at https://api.slack.com/apps -> **From a manifest** -> paste
   `slack_app_manifest.yaml`. (Socket Mode is preset, so no public URL.)
2. Install to the workspace. Copy the **Bot token** (xoxb-).
3. Enable Socket Mode and generate an **App-level token** (xapp-) with the
   `connections:write` scope.
4. Create the channel `#echoclaude` and invite the Echo bot to it.
5. Set both tokens in Railway by hand: `AGENT_SLACK_BOT_TOKEN` (xoxb-) and
   `AGENT_SLACK_APP_TOKEN` (xapp-), plus `AGENT_SLACK_CHANNEL_ID` for #echoclaude.
6. Test with the `/echo-draft` slash command. Cards land in #echoclaude with
   Approve / Edit / Skip. Only the approver's taps do anything.

### Meta (your app already exists: "LASSO Social Poster")

- **Keep the app in Development mode for Stage 1.** As the app admin you can
  publish to LASSO's own Pages and IG accounts without App Review. App Review +
  Live mode are only needed to post on accounts you do not own (clients, later).
- Requirements: the IG account must be Business or Creator and linked to a
  Facebook Page. Personal IG profiles are not supported by the API. Personal
  Facebook profiles cannot be published to at all (use a Page).
- Permissions for the Page-linked path: `pages_show_list`,
  `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`,
  `instagram_content_publish`. (Meta renamed some IG scopes to
  `instagram_business_*` in 2025; the dashboard may show either. Request what it
  offers for the Page-linked publishing path.)
- Generate a long-lived token for each account and set it by hand in Railway
  (`AGENT_LASSO_IG_TOKEN`, `AGENT_LASSO_FB_TOKEN`, etc.). Tokens last ~60 days and
  must be refreshed. Never paste a token into chat or code.

---

## Env vars

Copy `.env.example` to `.env` and fill by hand. Never commit `.env`.

- `AGENT_ENABLED`, `AGENT_PUBLISH_ENABLED` — the two flags above.
- `AGENT_APPROVER_SLACK_ID` — defaults to Blake.
- `AGENT_SLACK_BOT_TOKEN`, `AGENT_SLACK_CHANNEL_ID` — Slack control surface.
- `AGENT_VOICE_DOC_PATH` — defaults to `brand_voice/lasso_voice.md`.
- `AGENT_LIBRARY_PATH` — defaults to `content_library/`.
- Per-account tokens + target ids (set by hand, never logged):
  - `AGENT_LASSO_IG_TOKEN`, `AGENT_LASSO_IG_USER_ID`
  - `AGENT_LASSO_FB_TOKEN`, `AGENT_LASSO_FB_PAGE_ID`
  - `AGENT_BLAKE_PERSONAL_TOKEN`, `AGENT_BLAKE_PERSONAL_ID`

Tokens are read from env at the moment they are used, never stored on an object,
never written to any log. The post log has a field allowlist that cannot contain
a token.

---

## Brand voice doc (required)

Echo drafts **only** from the approved brand bible plus the client-provided note
on each creative. It never invents an offer, a price, a stat, or a client name.

- Path: `brand_voice/lasso_voice.md` (override with `AGENT_VOICE_DOC_PATH`).
- This is the **canonical LASSO Brand Bible**, synthesized from the LASSO AI Social
  Media Agent Brand Document, the V3 Style Guide, the Blake writing style profile,
  and the Gym Marketing Made Simple knowledge base.
- **If the voice doc is missing or empty, Echo drafts nothing** and posts a notice.

### Cadence note (Stage 1 vs the full standard)

The brand bible documents the full LASSO standard: 2 Instagram posts/day plus
mandatory cross-distribution. **Stage 1 deliberately ships a reduced cadence: one
feed post per day per account, draft-only, hold for approval.** On purpose, to
prove the voice and the gates before volume. The calendar grows toward the full
standard as trust is earned per account.

### Hard guardrail to never trip

LASSO is a lead-gen company. Echo must NEVER imply gyms do not need more leads (the
old "you don't have a lead problem" line is banned for the LASSO brand). See the
brand bible, section 4.

### Progress tracking

`PROGRESS.md` is the living build tracker (source of truth). `echo_build_tracker.html`
is the on-brand visual view.

---

## Content library (Stage 1, local)

Stage 1 reads a local folder, not the portal. Drop creative in `content_library/`.
Optional sidecar (`.json` or `.txt`) carries client-provided facts. See
`content_library/README.md`. Portal wiring is a later stage (left as a clean stub).

---

## What's built vs stubbed

**Built (Stage 1):** daily draft → Slack approval card → on-approve publish, with
all gates, the per-account trust ladder, logging, and draft-only safety.

**Stubbed (documented, not built — clean hooks in `agent/stubs.py`):**
- Stories posting
- Comment handling (Tier 1 auto-safe / Tier 2 surface-for-human / no auto DMs)
- 30-day creative refresh loop (the eventual product)
- Portal creative-library read + portal reporting write
- Nightly "brain" read (`agent/brain.py`): reads the LASSO Obsidian vault to get
  smarter on angles. Defaults OFF. Proposes only. NEVER auto-edits the voice doc
  (gate 5: human owns voice). Point it at your vault path to wire it.

Each stub raises `NotImplementedYet` so nothing half-runs, and documents the gate
it must honor when it ships.

---

## What Blake must do BY HAND (not done in code)

These are setup/approval steps only you can do. The code does not attempt them.

1. **Meta publishing access.** Create a Facebook App and get Graph API content-
   publishing approved. Permissions needed:
   - `instagram_content_publish` — publish to IG.
   - `pages_manage_posts` — publish to a Page.
   - `pages_read_engagement` — read Page/post engagement (for later reporting).
   - `instagram_basic` + `pages_show_list` — link the IG account to the Page.
   - App Review + a Business verification are typically required before these
     work on real accounts.
2. **Set each account's token + target id** as environment variables by hand
   (see `.env.example`). Never paste tokens into chat or into code.
3. **Write the LASSO brand voice doc** and drop it at `brand_voice/lasso_voice.md`.
4. **Arm publishing** (`AGENT_PUBLISH_ENABLED=true`) only when the drafts look
   right. Ship state leaves it OFF.

### Honest Meta limits (read before arming publish)

- **Instagram** requires an IG **Business or Creator** account linked to a
  Facebook Page. Personal IG accounts cannot use the content-publishing API. The
  creative must be reachable at a **public URL** — Meta fetches `image_url` /
  `video_url`. Local library files must be hosted first (set `public_url` in the
  creative's sidecar). Publishing is a two-step container → publish call.
- **Facebook Page** publishing is supported (photo / feed).
- **Personal Facebook profile** publishing is **not possible** via the Graph API
  (`publish_actions` was removed in 2018). The publisher raises `NotSupported`
  for a personal profile. Use a Page or an IG Business/Creator account for
  "personal" brand posting. In draft-only mode this never triggers; it only
  matters once publish is armed.

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite encodes the gates: one draft per account, approval required before
publish, draft-only never writes to Meta, missing voice doc blocks drafting,
non-approver denied, new accounts start at full approval, no fabrication in
captions, tokens never logged.

---

## Architecture map

```
agent/
  config.py          flags, approver gate, paths
  accounts.py        account registry; tokens read lazily from env, never logged
  trust.py           per-account trust ladder (full-approval in Stage 1)
  voice.py           brand voice loader; blocks drafting if missing
  library.py         Stage 1 local content library + creative selection
  drafter.py         composes caption + hashtags from voice + client note only
  slack_surface.py   Ranger-style approval card poster + reply parser
  approvals.py       the hard gate: Approve / Edit / Skip
  meta_publisher.py  Graph API publish, with the draft-only guard inside
  postlog.py         append-only log for reporting (no tokens, ever)
  runner.py          daily job: one draft per account → Slack
  brain.py           nightly Obsidian "brain" read (later; proposes, never rewrites voice)
  stubs.py           documented hooks for later stages
```
