# Stage 2 runbook: onboarding client one (ordered, by hand)

Every step below is Blake by hand. Echo's flags all default OFF, so nothing here
fires until the step that arms it. Do the steps in order; verify each before the
next.

## 0. Prerequisites (once, not per client)
- [ ] Meta App Review approved for the permissions in docs/META_APP_REVIEW_KIT.md
      (submit early: this is the long pole, days to weeks).
- [ ] The intake web service exists on Railway as its OWN service (start command:
      `/opt/venv/bin/python -m agent intake-web`; same repo, no /data volume; R2
      creds env only). Leave AGENT_INTAKE_ENABLED unset there until step 5.

## 1. Slack
- [ ] Create the client's approval channel (for example #echo-<client>).
- [ ] Invite the Echo app and the approver(s).
- [ ] Note the channel id and the approver Slack id(s).

## 2. Intake conversation and brand bible
- [ ] Copy brand_voice/BRAND_VOICE_INTAKE.example.md and fill it WITH the client.
- [ ] Run: `/opt/venv/bin/python -m agent draft-bible --client <key> --intake <path>`
- [ ] Review both drafts under brand_voice/drafts/<key>/: fill every TODO, verify
      every social proof entry (permission AND a real verified date).
- [ ] Activate by hand: copy the reviewed files to the client's configured
      voice_doc and social_proof_doc paths. Nothing activates them but this copy.

## 3. Account entry (code, one small PR)
- [ ] Add the client's Account entries in agent/accounts.py: key, platform, token
      env NAMES, voice_doc, social_proof_doc, library_prefix, slack_channel,
      approvers. trust stays FULL_APPROVAL (the default; do not touch).

## 4. Tokens and ids (Railway listener service env, by hand)
- [ ] Client IG/Page access tokens + target ids under the env NAMES from step 3.
- [ ] Verify: `/opt/venv/bin/python -m agent check-tokens` shows the new tokens
      healthy (never prints values).
- [ ] Capture the pre Echo baseline NOW, before any Echo post:
      `/opt/venv/bin/python -m agent capture-baseline`.

## 5. Texted link intake
- [ ] Generate a long random token; set AGENT_INTAKE_TOKEN_<CLIENTKEY> on BOTH the
      intake web service and the listener service (same value, by hand).
- [ ] Set AGENT_INTAKE_ENABLED=true on both services.
- [ ] Text the client their private link: https://<intake-domain>/u/<token>
- [ ] Verify: upload one test photo with a note; within the poll window the file
      appears in the client's library prefix with its .txt note beside it, and the
      test draft card cites it.

## 6. Activation order of flags (listener service, one at a time)
1. AGENT_ENABLED=true (already on for LASSO; confirms the client accounts draft).
   Verify: the client's daily card arrives in THEIR channel, draft only.
2. AGENT_CONTENT_BRAIN_ENABLED / AGENT_NANO_ENABLED / AGENT_HOSTING_ENABLED as
   desired for generated cards. Verify: card quality for several days.
3. AGENT_SOCIAL_PROOF_ENABLED=true once their social_proof.md is verified.
   Verify: the weekly proof card uses only verified entries.
4. AGENT_GRADE_ENABLED=true when reporting inputs exist.
   Verify: the grade line is honest (gaps listed, no fake green).
5. LAST and only after days of good drafts: publishing for the client is governed
   by the same AGENT_PUBLISH_ENABLED as everything else. Announce to the client
   that approval taps now publish for real.

## 7. What to verify at each step
- After every flag: `/opt/venv/bin/python -m agent status` shows exactly the flags
  you intended, nothing else drifted.
- Every draft waits for approval in the CLIENT'S channel; a non approver tap is
  denied; skip drops the draft.
- No secret ever appears in Slack, logs, or git (tokens are env only).
- The /data volume stays attached ONLY to the listener service; the intake web
  service must never reference it.
