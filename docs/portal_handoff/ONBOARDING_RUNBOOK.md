# Onboarding Runbook

How to add a gym to Echo. Every step is in order. Steps marked **BLAKE** require a human decision or hand action by Blake.

Worked example: District H (`districth`).

---

## Step 1: Run add-client

```
python -m agent add-client --key districth --name "District H"
```

This creates:
- `brand_voice/districth/lasso_voice.md` (voice doc template, all TODOs)
- `brand_voice/districth/social_proof.md` (empty proof doc)
- `content_library/districth/.gitkeep`
- Prints an `Account(...)` config entry to paste into `agent/accounts.py`.

Nothing is armed. Nothing publishes. The command is idempotent (re-running skips files that exist).

---

## Step 2: Paste Account entry into accounts.py

**BLAKE:** Paste the printed Account entry into `agent/accounts.py` ACCOUNTS list. Fill:
- `slack_channel`: the gym's approval Slack channel id.
- `approvers`: the approver's Slack user id(s).
- `token_env`, `target_id_env`: the env var names (they exist in the template; do not invent new names).

Commit and deploy.

---

## Step 3: Set tokens in Railway env (never in git)

**BLAKE:** In Railway, set:
- `AGENT_INTAKE_TOKEN_DISTRICTH`: the gym's intake token (a random URL-safe string, at least 16 chars). This is the token the portal stores and sends.
- `AGENT_DISTRICTH_IG_TOKEN`: the gym's Meta Graph API access token.
- `AGENT_DISTRICTH_IG_ID`: the gym's Instagram account id.

Tokens are never in git, never in this repo, never in chat. Set them only in Railway environment variables.

---

## Step 4: Hand the token to the portal (by hand, never via code or chat)

**BLAKE:** Copy the token value from Railway. Give it to the portal CC via a secure channel (1Password note, or paste directly into the portal's Vercel env / Supabase secrets). The portal stores the token as an encrypted secret keyed to this gym. It sends the token in the URL path on every Echo call.

The token value never appears in:
- Any git commit
- Any Slack message
- Any Claude Code conversation

---

## Step 5: Verify tokens

```
python -m agent check-tokens
```

This confirms the tokens are set and resolvable. It never prints values.

---

## Step 6: Capture the pre-Echo baseline

**BLAKE:** Before any Echo post goes out, capture the gym's social baseline:

```
python -m agent capture-baseline
```

This writes `/data/baseline_<month>.json`. It is the "before Echo" number used in the 30-day report and social grade. Run it once, before any Echo post publishes.

---

## Step 7: Fill the voice doc and verify social proof

**BLAKE:** Fill `brand_voice/districth/lasso_voice.md`. Every TODO section must be completed before drafting begins. An unfilled voice doc blocks drafts (the gate is explicit).

For social proof entries in `brand_voice/districth/social_proof.md`: each entry must have `Permission: yes` and a `Verified: YYYY-MM-DD` date. Entries without these never render.

---

## Step 8: Send the gym their intake link

Give the gym their intake URL: `https://echo-intake-web-production.up.railway.app/intake/<token>`.

Or, in the portal: the onboarding wizard POSTs to Echo and shows the gym the upload link after they complete the form.

---

## Step 9: Shadow week

Drafts only, no publishing. Approve or skip each day's drafts in Slack. Tune the voice doc based on what the drafts get right and wrong.

---

## Step 10: Arm publishing (when ready)

**BLAKE:** Set `AGENT_PUBLISH_ENABLED=true` in Railway for this account (or globally when all accounts are ready). Trust stays at level 0 (full approval). Every post still needs a human tap.

---

## Step 11: Arm reporting (when reporting inputs exist)

**BLAKE:** Set `AGENT_REPORTING_ENABLED=true`. Run `capture-baseline` if not already done for this gym.

---

## District H Quick Reference

| Item | Value |
|---|---|
| Client key | `districth` |
| Intake URL | `https://echo-intake-web-production.up.railway.app/intake/<token>` |
| Token env name | `AGENT_INTAKE_TOKEN_DISTRICTH` |
| IG token env name | `AGENT_DISTRICTH_IG_TOKEN` |
| IG id env name | `AGENT_DISTRICTH_IG_ID` |
| Voice doc | `brand_voice/districth/lasso_voice.md` |
| Approval Slack channel | Set on Account entry in `agent/accounts.py` |
