# Independent release adoption

Use this operator command only after a separate reviewer verifies the deployed
reported fixes. It adopts independently built Echo and portal releases into an
existing current Echo Slack code ticket. It neither sends Slack nor resolves the
ticket. A successful write makes the normal FIXER/outbox completion eligible.

The original PR's CI receipts remain in
`verification_before.independent_release_adoptions`. They are never reused as
the accepted release's test results. The new after record explicitly attributes
the adoption to `independent_codex_release_adoption` and retains the review file's
SHA256, reviewer and builder IDs, limitations and actual provider readbacks.

## Inputs

The manifest has `ticket_id`, `request_version`, `superseded_pr_url`,
`railway_project_id`, optional absolute `railway_directory` pointing to an already
linked project context, `client_note`, `limitations`, `releases` and `business_params`.

`releases` must contain exactly one Echo PR and one portal PR, each with only
`pr_url` and the **actual PR merge commit** as `merge_sha`. GitHub is queried for
the merge and the head's successful `pytest` or `portal-gate` checks. Railway's
newest production deployments for both Echo services and Vercel's current
production alias are queried. Deployed descendants are verified by GitHub
compare. The expected merge SHA and actual deployment SHAs remain distinct.

`business_params` is `{request_id, asset_ids, approved_cta}`. Use the original reel
UUID and three to ten exact gym-owned usable raw video IDs seen in the actual
picker. The CTA must still appear in the current approved gym voice. Neither
asset IDs nor CTA expectations are treated as proof.

The separate JSON review file has `schema_version: 1`, `ticket_id`,
`request_version`, the freshly recomputed `request_key`, `verified: true`,
distinct `reviewer_id` and `builder_id`, an explicit UTC `checked_at`,
`release_identifiers` exactly equal to the manifest's releases,
`business_params` exactly equal to the manifest's expectations,
`essential_failures: []`, and `client_note` and `limitations` exactly equal to the
manifest's reviewed message and remaining limits. Changing either requires a new
review receipt. Preserve actual review identity;
the command validates this receipt's bindings but cannot authenticate a human's
claim that two reviewer names represent independent people.

The client note must accurately describe the verified reported scope. A portrait
framing hold, unfinished reel, future client approval or publishing, and a
separate retry-ledger repair are explicit limits when applicable. Do not claim
reel completion, approval or platform delivery from this business check.

## Run

Use the accepted code with Python dependencies already installed. The host needs
authenticated `gh`, `railway` and `vercel` CLIs, existing Supabase configuration
for `agent.slack_convo.bus.Bus`, and `FIXER_OPS_SECRET` for the authenticated
read-only Echo worker observer. No keys are printed or embedded in artifacts.

```
python3 -m agent.support_release_adoption --manifest /absolute/manifest.json --review /absolute/review.json
```

This is a dry run. The worker observer compares the fresh shared portal mirror
against worker-owned job state for the original UUID. Missing, stale or divergent
state refuses adoption. A job still reporting the original zero-ask/approved-CTA
failure also refuses adoption even if its client stopped message is truthful.
Actual thumbnail responses must decode as bounded images
through the tenant's token-scoped endpoint; the capability token never enters
the returned evidence. A genuine safety hold can coexist with verified status
and picker repairs. This check does **not** prove reel completion.

Only after independent acceptance and a successful dry run, add `--write` to
perform one CAS. It compares full request/tenant/Slack identity, old PR and both
verification JSON columns. A changed requester message or concurrent receipt
writer refuses the write without overwriting evidence. The ticket stays open
until the existing outbox confirms the actual Slack post by readback and closes
the same request through `fixer_resolve_current_delivery`.

Do not close the superseded PR while its merge poll still owns the ticket:
normal FIXER handling of a closed unmerged PR escalates the ticket to hold.
Adopt first after acceptance, verify the readback, then retire the superseded PR
through the owner's normal workflow. An existing hold remains a release hold;
the adoption command cannot waive it.
