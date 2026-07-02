# Meta App Review kit (Echo, done for you posting with mandatory human approval)

Everything Meta's reviewer needs, derived from the Graph calls Echo actually makes
(file references below point at the code so nothing here is guessed).

## 1. Permissions requested and exactly why (derived from code)

| Permission | Why Echo needs it | Where in code |
|---|---|---|
| instagram_content_publish | Publish approved feed posts, Reels, carousels, and Stories to the client's IG professional account: POST /{ig}/media (image, REELS, CAROUSEL, STORIES containers) then POST /{ig}/media_publish | agent/meta_publisher.py (publish paths), agent/stories.py |
| instagram_basic | Read the linked IG account and its media: GET /{ig}/media for the pre launch posting baseline; GET /{media_id}?fields=permalink for the post publish verification read | agent/baseline.py, agent/publish_confirm.py |
| pages_manage_posts | Publish approved photo and feed posts (and photo Stories) to the client's Facebook Page: POST /{page}/photos, POST /{page}/feed | agent/meta_publisher.py (_publish_fb_page), agent/stories.py |
| pages_read_engagement | Read the Page's own posts for the posting baseline (GET /{page}/posts) and the permalink_url verification read after a publish | agent/baseline.py, agent/publish_confirm.py |
| pages_show_list | List the Pages the user manages so the right Page is linked during onboarding | onboarding (token + Page id setup) |

Also used, no extra permission: GET /debug_token (agent/token_watchdog.py) to warn
before a token expires. Not requested yet: insights permissions; Echo's reporting
reads are dormant behind a flag and will be a separate request when armed.

## 2. Use case narrative

Echo is a done for you social posting service for gym owners run by LASSO. A human
marketer drafts every post with the client's own photos, videos, and words. EVERY
post then waits in Slack for explicit human approval (approve, edit, or skip) from
a named approver before anything publishes; there is no fully automatic publishing
path. On approval Echo publishes to the client's own IG professional account and
Facebook Page via the Graph API, verifies the post with one read, and reports the
permalink back to the approver. Clients hand their media to Echo directly; nothing
is scraped and no third party content is used.

## 3. Reviewer test instructions (step by step)

1. Log into the test workspace we provide (Slack channel #echoclaude equivalent).
2. Trigger a draft: the service posts an approval card containing the exact image
   and caption that would publish.
3. Observe that NOTHING publishes without action: wait, no post appears.
4. Tap Approve as the named approver. The service publishes that one post to the
   linked test IG account or test Page.
5. Observe the confirmation reply in the card thread: LIVE: followed by the post's
   permalink. Open it and compare: the published content matches the approved card.
6. Tap Skip on a second card: no post is created.
7. Attempt approval from a non approver account: the action is denied.

## 4. Screencast shot list

1. The approval card in Slack: image preview, caption, Approve, Edit, Skip.
2. The linked IG account before approval (post absent).
3. Tapping Approve as the approver.
4. The LIVE permalink reply appearing in the thread.
5. The published post on the IG account, matching the card.
6. Tapping Skip on another card, then the account unchanged.
7. A non approver tapping Approve and being denied.

## 5. Data handling answers

- What data is accessed: the client's own Page and IG account ids, their access
  tokens, the media and captions the client supplied, and the ids and permalinks of
  posts Echo itself published.
- Storage: tokens live only in environment variables set by hand on the hosting
  platform; they are never logged, never written to disk, never committed. Media
  lives in the client's own content library (private object storage). Post logs
  store captions and post ids, never tokens.
- Sharing: no data is shared with any third party. No data is sold.
- Deletion: removing a client deletes their library prefix, their tokens (env), and
  their pending drafts. Published posts belong to the client's own accounts.
- Human oversight: every publish requires named human approval; the approver id is
  pinned in configuration (agent/approvals.py).
