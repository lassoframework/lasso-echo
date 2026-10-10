# GBP listener Drive use recovery

Local caller slice. Default OFF via `AGENT_GBP_DRIVE_USE_RECOVERY`. No flags,
production services, migrations, providers or senders were enabled or changed.

The daily runner invokes recovery immediately after its master guard and before
voice loading. This includes the no-voice early-return branch. Recovery is
independent of the month-sweep flag, city, connection discovery, future-row
absence and new-month eligibility. It never generates or inserts content.

## Original journal identity and explicit operator enrollment

An existing absolute non-ephemeral path is necessary but insufficient. Recovery
requires a journal instance UUID stored in `gbp_drive_use_recovery_owner` AND an
exact independently configured `AGENT_GBP_DRIVE_USE_RECOVERY_JOURNAL_ID` pin.
Missing metadata, missing external pin, mismatch, missing tables or missing
volume holds without enrolling a replacement SQLite file.

The explicit local helper `pin_original_journal(instance_id)` is an operator
step only, never called by recovery. After independently verifying the original
listener volume and frozen journal, an authorized operator supplies a fresh
canonical UUID to that helper and retains the same UUID independently in the
listener deployment config. The helper requires an existing file with both
journal tables; it creates only the recovery metadata and ordering index,
cannot overwrite an existing different identity, and never opens missing files
in create mode. Operator pinning has NOT been performed in production.

Path resolution and metadata do not prove production mount provenance. A full
copy of the pinned SQLite database retains its identity; original-volume
verification and preventing a copied journal from running concurrently remain
production topology requirements. Never auto-pin an arbitrary replacement DB.

## Bounded fair restart scheduling

Per daily invocation: at most 32 use entries, 8 tenants and 16 batch receipt
lookups. The recovery metadata also holds a durable `(created_at,use_id)`
cursor. Enumeration reads the next bounded circular slice without advancing
the cursor. Local frozen batch discovery admits each row against tenant and
batch budgets. On reaching either budget, the wave stops BEFORE the first
unadmitted row, leaving it first for the next invocation. For every admitted
attempt, a separate transaction advances the cursor BEFORE claim/readback/PG
and remote settlement work. Held local-preflight attempts also advance fairly.
A crash on a held admitted entry cannot repeatedly restart the oldest slice.
No unresolved use is removed or released. Completed entries naturally leave
the worklist. A lost local cursor acknowledgment holds this run; a later run
uses the durable cursor that survived.

Local recovery queries are capped at 100,000 SQLite VM instructions per
connection and a two-second lock timeout. Calendar reads cap their result at
two rows and require exact counts on the normal REST adapter. Identity is
checked in enumeration, cursor, batch admission, original-claim and unique-member
connections. The live unique member must belong to the admitted frozen batch.

For each entry, recovery checks the original same-account claim and fresh exact
ACTIVE row, finds exactly one local frozen member binding, checks tenant and
member payload, then calls `bind_forward_finalization(batch_id)`. Only shared PG
terminal proof that the existing adapter verifies can become local finalized
proof. Recovery requires `forward_remote_use_settlement_allowed`, re-reads the
exact ACTIVE row after binding, rechecks flags and claim, and resumes existing
settlement with the same use UUID, asset/source snapshots and claim. Failed
binding holds every use of that batch in this invocation, even if an older local
finalized proof exists. Unknown, missing, staged, wrong-tenant, wrong-digest and
wrong-member receipts make no remote use.

The finalizer only owns PG finalization. It must not mount, share or open the
planner/listener SQLite journal. Recovery never invokes a finalizer. Production
topology, explicit original-journal pinning, applied migrations, current
reservation-lineage authority and a composed real-PG test with a separate
finalizer owner remain unverified. Keep the recovery flag OFF until those
checks are independently accepted.

Focused evidence uses the real binding adapter with synthetic PG RPC responses
and local SQLite. Tests cover actual daily no-voice execution, master-OFF,
replacement-journal rejection, the 33rd valid use recovering after restart
behind 32 held entries, and the ninth tenant/seventeenth batch becoming the
first attempted use after restart when the previous wave exhausted its budget.
Focused command `python3 -m pytest -q tests/test_gbp_drive_use_recovery.py
tests/test_gbp_staged_journal_binding.py tests/test_gbp_planner.py` passed 178
tests. This is not production or composed real-PG proof.
