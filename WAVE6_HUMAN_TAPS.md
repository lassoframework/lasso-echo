# Echo Human Taps Required (Waves 6-7)

These actions cannot be performed autonomously. Each requires Blake's explicit
decision and a Railway variable change. None is gated behind code; the infrastructure
is fully built and waiting. TAP 1 and TAP 2 are Wave 6; TAP 3 arms the Wave 7
learning loop.

---

## TAP 1: Second Publisher Disconnect

**Status: DONE — executed 2026-08-26 on Blake's explicit authorization.**
Duplicate Zernio IG account 6a74b3efd0fe733d1abc6fc1 disconnected (grace period
ended 2026-08-26 21:18Z; 535 external post mirrors cleaned, 0 scheduled posts
lost). accounts_list verified: exactly one lassoframework Instagram remains
(6a69fc9cdf17280d93d0727f). Remaining follow-ups: audit Meta Business Suite
native scheduler for residual queued posts; monitor the 14:10 ET pattern for
one week to confirm it stops.

See wave0_publisher_finding.md for the evidence and recommendation.
Blake must confirm which Zernio account to disconnect before we proceed.

To review the current connected accounts:

```
railway variables --service echo
```

Do NOT disconnect any account until Blake has reviewed and confirmed the target.
The code will not disconnect anything automatically.

---

## TAP 2: Per-Gym AGENT_CALENDAR_GRADE Flag Flips

**Status: DONE — executed 2026-08-26 on Blake's authorization ("do 2").**
All five per-gym flags + the global default are ARMED on the echo Railway
service. Sweeps + digests ran clean for every gym. Baseline grades (all
pre-A-gate content, expected low): lasso F36/F22, eng F14/F25, gritx F37/F37,
piercefitness F34/F37, topfuel D60/F40. The nightly sweep now runs in the
daily draw; forward books rise to A as the planner restages under the gate.

Rollout order per spec. Set each flag in Railway one at a time, then run the
sweep and digest before moving to the next gym:

### Step 1: LASSO (internal dogfood first)
```
# Railway env set:
AGENT_CALENDAR_GRADE_LASSO=true

# Then verify:
python3 -m agent jobs grade_sweep lasso
python3 -m agent jobs rollout_digest --gym lasso
```

### Step 2: CrossFit ENG
```
AGENT_CALENDAR_GRADE_ENG=true

python3 -m agent jobs grade_sweep eng
python3 -m agent jobs rollout_digest --gym eng
```

### Step 3: GritX
```
AGENT_CALENDAR_GRADE_GRITX=true

python3 -m agent jobs grade_sweep gritx
python3 -m agent jobs rollout_digest --gym gritx
```

### Step 4: Pierce Fitness
```
AGENT_CALENDAR_GRADE_PIERCEFITNESS=true

python3 -m agent jobs grade_sweep piercefitness
python3 -m agent jobs rollout_digest --gym piercefitness
```

### Step 5: TopFuel
```
AGENT_CALENDAR_GRADE_TOPFUEL=true

python3 -m agent jobs grade_sweep topfuel
python3 -m agent jobs rollout_digest --gym topfuel
```

### Step 6: New onboards (global default ON)
Once all existing gyms have been individually confirmed, flip the global default so
every new onboard inherits grade enforcement automatically:
```
AGENT_CALENDAR_GRADE=true
```

---

## TAP 3: Wave 7 Learning Loop Flags (AGENT_METRICS_SYNC, then AGENT_LEARNING_LOOP)

**Status: STEPS 1 + 3 FLAGS ARMED 2026-08-26 on Blake's authorization. Step 2
(the closed-month wait) is in progress; the first monthly_retro on real data
has still NEVER run and stays a reviewed manual run.**

Executed 2026-08-26: AGENT_METRICS_SYNC=true (nightly pull wired into the daily
draw; calendar join fixed and verified live — Echo posts land matched, unknown
posts land external and never train). AGENT_LEARNING_LOOP=true (lever stamping,
experiment labeling, and playbook consumption armed — the playbook is empty so
planner behavior is unchanged until the first retro writes one).

REMAINING: wait for the first FULL CLOSED MONTH of clean metrics (September
2026 is the first candidate; August is tainted by the second publisher), then
run the first monthly_retro manually per Step 3 below and review before
trusting it. monthly_retro is deliberately NOT scheduled anywhere.

Order is strict, per gym: METRICS FIRST, RETRO ONLY AFTER A FULL CLOSED MONTH OF
CLEAN METRICS.

### Step 1: Arm the nightly metrics pull (read only)
```
# Railway env set:
AGENT_METRICS_SYNC=true

# Then verify the pull (read only, writes post_metrics rows, publishes nothing):
python3 -m agent status                 # metrics_sync should read true
python3 -c "from agent import metrics_sync; import json; print(json.dumps(metrics_sync.run(), indent=2, default=str))"
```
Check post_metrics in Supabase: rows deduped by platformPostId, external posts
flagged external=true with calendar_id null. Let this run nightly.

### Step 2: Wait for a FULL CLOSED MONTH of clean metrics
Do not proceed until an entire calendar month has day-1/3/7/28 snapshots and the
duplicate-connection dedupe has been eyeballed. TAP 1 (the second publisher)
matters here too: an active second publisher taints every month it touches, and
a tainted month is observed but never trained on.

### Step 3: Arm the learning loop (stamping, playbook consumption, experiments, retro)
```
AGENT_LEARNING_LOOP=true

# First retro run — ON THE CLOSED MONTH ONLY, review output before trusting it:
python3 -m agent.jobs.monthly_retro --month YYYY-MM --gym lasso
```
Roll out gym by gym (lasso first, the Wave 6 order). The retro writes a versioned
gym_playbook row with evidence behind every change; each weight moves at most
plus or minus 20% per month and any version can be reverted by reading the prior row.

### What these flags can NEVER do (enforced in code, regression tested)
- Touch quota floors, avatar rails, ask rules, consent rules, or the copy gate
  (agent/playbook.py PROTECTED_KEYS refuses them outright).
- Publish, approve, or bypass the human approval tap — every post still lands pending.
- Train on external posts, tainted months, or below-floor samples.

---

## Notes

- Per-gym env vars take precedence over the global AGENT_CALENDAR_GRADE flag.
  A gym with AGENT_CALENDAR_GRADE_LASSO=true runs enforcement even if the global
  flag is false; a gym with AGENT_CALENDAR_GRADE_ENG=false is exempted even when
  the global flag is true.
- All flag checks go through config.calendar_grade_enabled_for(gym_id) in
  agent/config.py. No code changes are needed to flip these flags.
- Every draft continues to land pending and requires a human approval tap.
  These flags only gate WHETHER a planned month is staged, not whether posts publish.
- If a gym scores below A (90) after 4 remediation passes, staging is blocked and
  an ops alert fires. Blake must investigate the grade defects before re-running.
