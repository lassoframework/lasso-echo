# ENG recovery — offline conditional schedule

Source: [Sep 25 16:05 CT row export](eng-rows-20260925.txt) and orchestrator addendum.
68 FB/IG rows: **42 Sep 19–25 rows (37 review-blocked + five others), then 26 future rows**.
GBP's two exported rows stay unchanged. Last FB/IG feed publication: Sep 18 17:30–17:31 CT.
Cadence: posts_per_day=2; autonomous=false; America/New_York.

**Review cutoff assumed only for this proposal: Sep 26, 2026 00:00 EDT (04:00 UTC).**
This is not a claim Blake has finished. First proposed slot is Sep 26 07:30 EDT
(11:30 UTC). Use this table only if asset review actually finishes by the cutoff;
otherwise rerun with the actual finish timestamp and replace the entire table
before applying. Every allocated slot must be strictly after review. No DB writes,
approvals, sends or publishing occurred.

One original day/slot bundle per new day keeps shared photos on one date.
This conservative schedule allows at most **two total rows/platform/day**, including
stories: usually one FB feed, one IG feed and one IG story. The two original stories
at 16:30 UTC are separated onto successive days, never stacked in one slot.
Original feed index 0 uses 11:30 UTC; index 1 uses 22:30 UTC; story uses 16:30 UTC.
Missing indices are explicitly proposed as 0. These are 07:30 / 18:30 / 12:30 EDT.
Missed bundles occupy Sep 26–Oct 9; future bundles shift to Oct 10–18.

## Review these assets first (deduplicated)

A01–A14 cover the entire missed backlog; A15–A23 cover shifted future rows.
Verify current hash-bound moderation, real image and consent for every asset;
none is auto-approved. Recognizable people require genuine release/member/expiry
evidence. Clean no-people assets still need human approval.

| Ref | Media asset ID |
|---|---|
| A01 | `1ISLY8etTldUnYaiA-_NuQb4ih_3aVByx` |
| A02 | `1IeLs9KFJecJaeS9A9O5kpZhZftBjfBvK` |
| A03 | `1JJ8Ij1KX77-G4lvWY_mPvun4tbpFTkmb` |
| A04 | `1J_xVfhYww0Nene2KBR-lU--p0_f8EVHH` |
| A05 | `1JbkMsrK0NJ57hTD3EaFJ2G_L2TF3r9ve` |
| A06 | `1JlW7EZSMaIgqtwN6C5ixqWTo7uE7p_97` |
| A07 | `1Jn0XUPS7d1iFp_hoDhiUdrl6s4uO0x0O` |
| A08 | `1JzhLF5DrtjOU-320OCqQS0wjpAhNY-vL` |
| A09 | `1K4PfaQQ5rS4DOtqY2F750xEDuiyiiWj0` |
| A10 | `1K9ShcrckXcGRu774jpilrEtK6O7SIwKb` |
| A11 | `1KMomDcCtsPhx1e38-gMuQA96XazVcope` |
| A12 | `1KNEEPhzg7DjUDxZeagt1QilueLDgLPER` |
| A13 | `1KOu-LBym_FLdYZgMB2eeOAo6_b8LpqEt` |
| A14 | `1KWqwxxOIkQxjco2cIDA3p4ft_-LhqiXF` |
| A15 | `1KyMLHofZY-J6LAXcRhfyJNlAYw9gvXUA` |
| A16 | `1KzqSt7rivUDlylPgSKNcchBsw-0-wcWN` |
| A17 | `1L3LG8-Ef-DOmA6AUuTPkCF6lcOE5pgPg` |
| A18 | `1LAioP9G4jh5o2S9SixOryz1Yv1xxsCOD` |
| A19 | `1LIZl7HnzI_snUGEtZ-GNmFnn5HaVRqpz` |
| A20 | `1LKh2znmh4T4OM9zfMYVT_VXzijcXcsq1` |
| A21 | `1AM_R952sdKirpSipm0MnvRsWebGE8DLt` |
| A22 | `1EyMHiJU9AND8zRMmaoEBIbKcuT8nquFl` |
| A23 | `1HdXBsirSUbImkfr_isjhn9pWTjUkHT61` |

Four future rows have no source media ID (shown as —); assign/review suitable media
before they become publishable. Rows `fc82bac7-dc88-4b59-b5ca-5c5d2f85f43d`,
`ad13192a-5b57-4011-ad29-53cc2f29763e`, and
`998ca871-68e2-4626-9792-44bbb05140f4` also need their `multi_ask` caption issue
corrected. These are reserved proposal slots, not clearance to publish.

## Literal row-by-row proposal (all dates 2026; times UTC)

Keep every ID/media binding. Treat moved rows as pending for manual date/content
review; no existing pending row becomes automatically approved. Old date/index is
shown for audit. Feed indices remain 0/1; missing indices receive proposed 0.

| Original date/index | Calendar row ID | Platform / format | Proposed UTC | Media |
|---|---|---|---|---|
| 09-19/0 | `7c3f42fc-cbf0-46ff-be3a-82a53aa8b2f2` | FB feed | 2026-09-26 11:30 | A01 |
| 09-19/0 | `9c20ac76-a560-4e52-8cde-30436925ca4d` | IG feed | 2026-09-26 11:30 | A01 |
| 09-19/0 | `258f1572-5141-4518-ad98-ca4eed473e24` | IG story | 2026-09-26 16:30 | A01 |
| 09-19/1 | `8b7da4b2-9d71-45d6-952a-c5b601d14c60` | IG story | 2026-09-27 16:30 | A02 |
| 09-19/1 | `82ffeb39-9581-4aea-a8ae-c8ddbdefbf62` | FB feed | 2026-09-27 22:30 | A02 |
| 09-19/1 | `df8e6793-5216-40c7-b1a0-9cff8bb3f6e8` | IG feed | 2026-09-27 22:30 | A02 |
| 09-20/0 | `7d27c79d-3e66-4dbc-a57a-2a67089ce088` | FB feed | 2026-09-28 11:30 | A03 |
| 09-20/0 | `0d3fd8ff-04d2-4472-86b8-943344ec0373` | IG feed | 2026-09-28 11:30 | A03 |
| 09-20/0 | `c896c7fb-42e9-4964-bf74-9d442d0a2982` | IG story | 2026-09-28 16:30 | A03 |
| 09-20/1 | `7874bc11-84f5-4064-bca6-d8f34d0f47a0` | IG story | 2026-09-29 16:30 | A04 |
| 09-20/1 | `7df9bb93-bbfb-4543-bd6c-0cbd698d0b4d` | FB feed | 2026-09-29 22:30 | A04 |
| 09-20/1 | `089e7d66-6335-42f8-8e1e-bf852b050f7d` | IG feed | 2026-09-29 22:30 | A04 |
| 09-21/0 | `3c7aee8d-cf1c-4de1-9cb5-dfc75594729e` | FB feed | 2026-09-30 11:30 | A05 |
| 09-21/0 | `b7d6384c-fd8b-4bf1-8922-b22ea1846823` | IG feed | 2026-09-30 11:30 | A05 |
| 09-21/0 | `9c80ffef-e7f3-4ddd-b850-4f6604c2a3b8` | IG story | 2026-09-30 16:30 | A05 |
| 09-21/1 | `ae83660c-660e-46a1-9a9b-5c199b9c636d` | IG story | 2026-10-01 16:30 | A06 |
| 09-21/1 | `47355bab-4033-40ed-892b-fad8cc9e7cd6` | FB feed | 2026-10-01 22:30 | A06 |
| 09-21/1 | `bc517a94-1f4d-456d-bcf0-af1c02bf8d2f` | IG feed | 2026-10-01 22:30 | A06 |
| 09-22/0 | `b3d7dfcd-664e-4b70-a3b4-bd91caacf110` | FB feed | 2026-10-02 11:30 | A07 |
| 09-22/0 | `4d3d2a4b-8eac-424c-90d5-9ef1221668cd` | IG feed | 2026-10-02 11:30 | A07 |
| 09-22/0 | `aabfff6d-e69e-4977-b22e-c32da9f8c53c` | IG story | 2026-10-02 16:30 | A07 |
| 09-22/1 | `65f5e4f7-ce68-43c3-8914-11c82dd4778c` | IG story | 2026-10-03 16:30 | A08 |
| 09-22/1 | `ce4fcb31-0ff2-443a-aed5-f611299307ed` | FB feed | 2026-10-03 22:30 | A08 |
| 09-22/1 | `5d3c1c74-2d16-4da0-90bb-0ab7aace26d8` | IG feed | 2026-10-03 22:30 | A08 |
| 09-23/0 | `061723fa-b2d3-4c65-bf32-3ec9b89b114f` | FB feed | 2026-10-04 11:30 | A09 |
| 09-23/0 | `f4ce2767-6ce7-413e-a4bb-7fab8aa51676` | IG feed | 2026-10-04 11:30 | A09 |
| 09-23/0 | `164146ba-e725-42a3-9d9d-ae588d7d0022` | IG story | 2026-10-04 16:30 | A09 |
| 09-23/1 | `415a44a0-b331-43d6-aa2a-6f617be9688d` | IG story | 2026-10-05 16:30 | A10 |
| 09-23/1 | `9b8f2bec-4ad2-41d2-a352-62445dc01311` | FB feed | 2026-10-05 22:30 | A10 |
| 09-23/1 | `6546fb8a-2f90-4bac-84a3-3e204b4a7439` | IG feed | 2026-10-05 22:30 | A10 |
| 09-24/0 | `65db6c55-8fba-46c4-a938-87e0ea74ceee` | FB feed | 2026-10-06 11:30 | A11 |
| 09-24/0 | `f45b8500-05f1-4733-9c3d-766d4f0d09d6` | IG feed | 2026-10-06 11:30 | A11 |
| 09-24/0 | `5f0a65b5-af1a-4a3b-820b-e6badc4a6a75` | IG story | 2026-10-06 16:30 | A11 |
| 09-24/1 | `07c4bd77-c3ea-435e-ad92-c9025bedf5d8` | IG story | 2026-10-07 16:30 | A12 |
| 09-24/1 | `91461810-d152-4d8e-a77a-961a51b621e5` | FB feed | 2026-10-07 22:30 | A12 |
| 09-24/1 | `71f998f4-928a-43d2-b30e-9e83b07a6bbc` | IG feed | 2026-10-07 22:30 | A12 |
| 09-25/0 | `c0f8dee9-a1fb-4e6f-bd3d-c06fed941ea0` | FB feed | 2026-10-08 11:30 | A13 |
| 09-25/0 | `f21d719f-8900-4271-841a-a7a6ef8b3373` | IG feed | 2026-10-08 11:30 | A13 |
| 09-25/0 | `f9fd0dbc-d415-4c9f-9879-6c075c0bf269` | IG story | 2026-10-08 16:30 | A13 |
| 09-25/1 | `6884694b-647b-4835-b1d4-64d41628ab26` | IG story | 2026-10-09 16:30 | A14 |
| 09-25/1 | `0303dfec-00e3-47cd-95cb-dc8ebdb6d263` | FB feed | 2026-10-09 22:30 | A14 |
| 09-25/1 | `84921fa4-2bdd-4cbb-bb1a-d469e7671e07` | IG feed | 2026-10-09 22:30 | A14 |
| 09-26/0 | `030d264c-f55e-422a-8075-09a526509daa` | FB feed | 2026-10-10 11:30 | A15 |
| 09-26/0 | `332c8cc7-b3c8-4309-b29c-74b92b7321f7` | IG feed | 2026-10-10 11:30 | A15 |
| 09-26/0 | `bcaec31c-aefb-42df-ab82-54e698ca692d` | IG story | 2026-10-10 16:30 | A15 |
| 09-26/1 | `0c3cb1da-6410-44bb-9777-f336845d754c` | IG story | 2026-10-11 16:30 | A16 |
| 09-26/1 | `81b9c13b-0b37-41b5-a182-65ccf7ea2a33` | FB feed | 2026-10-11 22:30 | A16 |
| 09-26/1 | `4cfd8c86-226a-4ece-890b-b7fb2ebca2de` | IG feed | 2026-10-11 22:30 | A16 |
| 09-27/0 | `53947e19-1507-4c50-bbbb-3bbec90d7d28` | FB feed | 2026-10-12 11:30 | A17 |
| 09-27/0 | `9e6a32fd-edfb-4739-9ef8-b995bc9358d7` | IG feed | 2026-10-12 11:30 | A17 |
| 09-27/0 | `c6dc93ba-418c-4a44-a418-e1bfa0b9ba43` | IG story | 2026-10-12 16:30 | A17 |
| 09-27/1 | `7499a64a-7c43-46a7-821d-0c763893d524` | IG story | 2026-10-13 16:30 | A18 |
| 09-27/1 | `3c54a244-856f-4f81-a951-7eb78ca7d1c4` | FB feed | 2026-10-13 22:30 | A18 |
| 09-27/1 | `ae81ef7d-fe4a-459e-9676-0e7e1c7981dc` | IG feed | 2026-10-13 22:30 | A18 |
| 09-28/0 | `1980621d-65d3-44d1-a9e0-f5d0a6e20103` | FB feed | 2026-10-14 11:30 | A19 |
| 09-28/0 | `c10aada1-ff2b-476d-a910-18417ce677c0` | IG feed | 2026-10-14 11:30 | A19 |
| 09-28/0 | `7300553b-d2af-4c78-abf1-7071e21c4302` | IG story | 2026-10-14 16:30 | A19 |
| 09-28/1 | `8382635a-3d5a-4bd9-9103-42ab57167c89` | IG story | 2026-10-15 16:30 | A20 |
| 09-28/1 | `680e58f8-93ff-4f9b-86a4-bbfadb80a7c2` | FB feed | 2026-10-15 22:30 | A20 |
| 09-28/1 | `e7a2dae5-a8c0-4268-bae6-5eea1d720362` | IG feed | 2026-10-15 22:30 | A20 |
| 09-29/0 | `5e473a53-9c8e-4c6c-b036-53aaeb033997` | FB feed | 2026-10-16 11:30 | A21 |
| 09-29/—→0 | `db275400-8202-41bf-ac9c-140a9ca5d937` | IG feed | 2026-10-16 11:30 | — |
| 09-29/—→0 | `3b38495c-d5ca-40e1-9204-eedffd5cf937` | IG story | 2026-10-16 16:30 | — |
| 09-30/—→0 | `43911246-a3e5-4c23-a78a-d1204905da85` | FB feed | 2026-10-17 11:30 | — |
| 09-30/0 | `fc82bac7-dc88-4b59-b5ca-5c5d2f85f43d` | IG feed | 2026-10-17 11:30 | A22 |
| 09-30/—→0 | `fd592056-f1d6-472b-9539-c8141eb1057e` | IG story | 2026-10-17 16:30 | — |
| 10-01/1 | `ad13192a-5b57-4011-ad29-53cc2f29763e` | FB feed | 2026-10-18 22:30 | A23 |
| 10-01/1 | `998ca871-68e2-4626-9792-44bbb05140f4` | IG feed | 2026-10-18 22:30 | A23 |

## Exact offline reproduction and operator boundary

From the repo root (no agent imports, credentials, network or DB access):

```bash
python tools/eng_catchup_plan.py docs/eng-rows-20260925.txt --rows-text --review-finished-at '2026-09-26T00:00:00-04:00' > eng-catchup-plan.json
```

If review ends later, replace that argument with its actual offset-aware timestamp.
The planner shifts the full queue; it does not preserve future rows ahead of missed
ones. The legacy JSON/reservation mode is not used for this recovery.

After separate deployment, Blake's existing interactive asset paths are:

```bash
python -m agent.jobs.moderate_gym_media eng --asset-id "$ASSET_ID"
python -m agent.jobs.moderate_gym_media eng --asset-id "$ASSET_ID" --apply
python -m agent.gym_media_review eng "$ASSET_ID" approve --note 'Inspected current image and evidence'
# People: only when actual consent references exist:
python -m agent.gym_media_review eng "$ASSET_ID" approve --release-ref "$RELEASE_REF" --member-ref "$MEMBER_REF" --expires-at "$CONSENT_EXPIRES_AT" --note 'Verified image consent'
```

These live-writing operator commands were **not run**. Do not batch-approve.
Keep publishing paused during review/application; registry `publish_flag` is not
the calendar kill switch (`AGENT_CALENDAR_AUTOPUBLISH` / `AGENT_PUBLISH_ENABLED`
are fleet-wide). Before applying, refresh all status/provider receipts and calendar
rows through Oct 18 (or the recalculated end date): this export stops Oct 1, so
any additional/new rows must join the tail and be rescheduled, never collide.
Do not reset publishing claims, delete rows, or enable a past-date backlog.
Apply the reviewed manifest through the calendar editor only after that inventory
check; approval, media reuse, caption, consent and normal publish guards still apply.
