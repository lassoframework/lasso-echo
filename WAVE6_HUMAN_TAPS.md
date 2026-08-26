# Wave 6 Human Taps Required

Two actions in this wave cannot be performed autonomously. Both require Blake's explicit
decision and a Railway variable change. Neither is gated behind code; the infrastructure
is fully built and waiting.

---

## TAP 1: Second Publisher Disconnect

**Status: PENDING BLAKE TAP**

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

**Status: PENDING BLAKE TAP (all gyms)**

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
