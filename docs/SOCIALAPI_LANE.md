# SocialAPI.ai publish lane

A second publish lane so Echo can post ORGANIC content (IG feed, IG Stories, FB
Page posts) for CLIENT gyms through SocialAPI.ai's already-approved Meta app,
without waiting on Blake's own Meta App Review. Per-account routing chooses the
lane. Drafting, the fabrication gate, the grade gate, Slack/portal approvals, the
trust ladder, and the calendar are all UNCHANGED. Only the publish step routes.

Status: BUILT, DARK. Every flag defaults OFF. LASSO's own accounts stay
`meta_direct`. Nothing about this lane can publish until Blake arms it by hand.

## Architecture

```
approve (Slack/portal)
  -> approvals.handle_action()            [gates: approver + publish_enabled]
     -> _publisher_for(account)           [the routing seam]
        - Google Business Profile         -> gbp_publisher
        - publish_route == "socialapi"    -> socialapi_publisher   (only when
          AND AGENT_SOCIALAPI_ENABLED         AGENT_SOCIALAPI_ENABLED is armed)
        - else (IG, FB)                   -> meta_publisher        (unchanged)
     -> postlog.log_post(...)             [same posts table, + permalink]
```

The "interface" is the module contract every lane already follows:
`publish(draft, account, http=None) -> PublishResult`, with the draft-only guard
living inside. `MetaDirectPublisher` is the existing `meta_publisher`;
`SocialApiPublisher` is the new `socialapi_publisher`. We matched the codebase's
duck-typed-module idiom rather than inventing a class hierarchy it does not use.

### Files

| File | Role |
|---|---|
| `agent/socialapi_client.py` | Thin REST client (brands, accounts/connect, media upload, posts, metrics). Bearer key read by name; never logged. |
| `agent/socialapi_publisher.py` | The lane. `publish(draft, account, http=None) -> PublishResult`. Draft-only + stories gates, idempotency, media upload, loud failure, per-day counter. |
| `agent/socialapi_store.py` | Per-account brand id + connected account ids at rest (Fernet when `AGENT_SOCIALAPI_ENC_KEY` set, else plaintext kv). |
| `agent/approvals.py` | `_publisher_for()` routing branch; passes `permalink` to postlog. |
| `agent/onboard.py` | Creates the gym's brand when `publish_route=socialapi` and the lane is armed. |
| `agent/intake_web.py` | `GET /portal/<token>/social-connect` and `/social-status`. |
| `agent/reporting_live.py` | `snapshot_socialapi_account()` — engagement-only metrics, honest gaps. |

## Routing

Per-account, via the `publish_route` field on the `Account` record
(`agent/accounts.py`): `"meta_direct"` (default) or `"socialapi"`. It is NOT an
env var. The `AGENT_SOCIALAPI_ENABLED` master flag must ALSO be on; with it off,
even a `socialapi`-routed account falls back to `meta_direct`. This is the safety
interlock: a stray field edit cannot move traffic on its own.

## Media (why bytes, not a URL)

SocialAPI ignores a raw public URL in `media_ids` (their docs: *"a raw public URL
is silently ignored"*). So the publisher fetches the approved creative's bytes
from its R2 `creative_public_url` and uploads them via `POST /media/upload` to get
a `media_id`, then references that in the post. R2 stays the sole source of truth;
no new hosting path is introduced (this is the one documented deviation from the
original spec line "publish uses R2 public_url", forced by the vendor).

## Captions

`text` passes through byte-for-byte identical to the Meta lane (same
caption + hashtags composition). Newlines are preserved verbatim (JSON encoding
keeps `\n`); `tests/test_socialapi_lane.py::test_publish_uploads_bytes_and_preserves_newlines`
asserts the round-trip.

## Idempotency / no double-post

Before publishing, the lane checks the `posts` table for an already-published row
for this `(draft_id, account_key)`. If present, publishing is a safe no-op that
returns the stored post id — no second network call. The draft id is also sent as
the vendor `Idempotency-Key`. Re-approving a card never double-posts.

## Failure modes

| Condition | Behavior |
|---|---|
| `publish_enabled()` OFF | `would_publish`, no network call (draft-only). |
| Story draft, `stories_enabled()` OFF | `would_publish` (double-gated, same as Meta). |
| No connected account for the platform | `SocialApiPublishError` raised (loud). |
| No `creative_public_url` | `SocialApiPublishError` (block, never fabricate). |
| Vendor status `failed`/`cancelled` | loud `ops_alerts.alert` naming account + draft, then raise. |
| Vendor still `publishing`/`scheduled` | raise `MediaNotReady` → approvals HOLDS the card for retry (same UX as Meta's async container). |
| > `AGENT_SOCIALAPI_MAX_PER_DAY` publishes/account/day | loud ops alert (the publish still succeeds; the alert flags the upstream bug). |
| Any REST non-2xx | `SocialApiError`, body scrubbed of secrets before it can surface. |

The API key is read by name every call, never logged, never stored on an object;
error bodies pass through `ops_alerts.scrub`.

## Reporting honesty

SocialAPI exposes per-post **likes, comments, saves, shares** only. It has **no**
impressions, reach, or follower count, and no account-level insights. So for a
`socialapi` gym:

- per-post rows fill only those four columns; `views`/`reach` stay NULL,
- the daily account snapshot carries a `{"data_source":"socialapi"}` marker and
  none of the reach/views/follower fields, so the monthly report renders them as
  explicit gaps (never a fabricated 0),
- the report must state its data source per account.

Impressions / reach / followers are **PLANNED** and blocked on the vendor
exposing them. Do not fake parity with the Meta lane.

## Onboarding runbook (District H is the worked example)

All by-hand steps are marked **BLAKE**.

1. **BLAKE** — set `AGENT_SOCIALAPI_KEY` in Railway env (never in code/git).
2. **BLAKE** — get written confirmation from SocialAPI.ai that their approved Meta
   app covers organic IG feed + Stories + FB Pages on client accounts.
3. **BLAKE** — set `publish_route="socialapi"` on the gym's `Account` in
   `agent/accounts.py` (District H: `districth_ig`, `districth_fb`). Commit.
4. **BLAKE** — set `AGENT_SOCIALAPI_ENABLED=true` in Railway.
5. Create the brand: `python -m agent socialapi-onboard --account districth_ig`
   (or it is created automatically on the next `onboard` run for that gym).
6. **BLAKE** — register the OAuth callback with the vendor and set
   `AGENT_SOCIALAPI_REDIRECT_URI`.
7. Hand the gym the connect links:
   `python -m agent socialapi-connect --account districth_ig` prints the IG + FB
   OAuth URLs. In the client portal, the same links come from
   `GET /portal/<token>/social-connect`.
8. The gym clicks each link and authorizes their IG + FB.
9. Verify: `python -m agent socialapi-status --account districth_ig` (or the
   portal `GET /portal/<token>/social-status`) shows both platforms connected.
   This also caches the connected account ids the publisher needs.
10. Approvals now publish through SocialAPI for that gym. Nothing else changes.

## Migration back to meta_direct (zero data-model change)

When Blake's own Meta App Review lands, flip a gym back by setting
`publish_route="meta_direct"` on its Account (or turning
`AGENT_SOCIALAPI_ENABLED` off globally). No draft, calendar, approval, or schema
change is required: the `posts` table is identical for both lanes, and `Draft`
has no route-specific field.
`tests/test_socialapi_portal_reporting.py::test_route_flip_zero_datamodel_change`
proves the flip both ways.

## Security

- `AGENT_SOCIALAPI_KEY` is env-only, read by name, never logged/printed/committed.
- Per-brand ids are encrypted at rest with Fernet when `AGENT_SOCIALAPI_ENC_KEY`
  is set (same pattern as intake tokens); plaintext kv is the dev fallback.
- Portal connect/status endpoints are token-isolated: the token HMAC-resolves to
  exactly one `account_key`, so gym A's token can never reach gym B's brand
  (`test_token_isolation`, `test_connect_isolated_by_token_resolution`).
- Non-SocialAPI gyms get a 404 from the connect/status endpoints, so a token
  never reveals routing the gym does not use.
