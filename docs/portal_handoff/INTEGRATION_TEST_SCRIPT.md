# Integration Test Script

Copy-paste end-to-end test against the live intake-web service. Nothing publishes. Requires a TEST token set in Echo's Railway env as `AGENT_INTAKE_TOKEN_TEST` with value `testtoken-portalhandoff-2026`.

Run these in order. All assertions are in comments.

---

## Prerequisites

Echo's Railway env must have:
```
AGENT_INTAKE_ENABLED=true
AGENT_INTAKE_TOKEN_TEST=testtoken-portalhandoff-2026
AGENT_INTAKE_PORTAL_ORIGIN=https://ops.lassoframework.com
```

Set these in Railway before running the test. Remove `AGENT_INTAKE_TOKEN_TEST` after the test.

---

## 1. Health check

```bash
curl -s https://echo-intake-web-production.up.railway.app/healthz
# Expected: {"ok": true, "intake_enabled": true}
```

---

## 2. Submit intake (portal JSON path)

```bash
curl -s -X POST \
  https://echo-intake-web-production.up.railway.app/intake/testtoken-portalhandoff-2026 \
  -H "Content-Type: application/json" \
  -d '{
    "gym": {"name": "Test Gym Portal Handoff", "locations": ["Test City, IN"], "website": "https://testgym.example.com"},
    "voice": {"vibe": "Direct and real", "words_to_use": ["strong"], "words_to_never_use": []},
    "offers": {"front_door_offer": "3 week trial", "services": ["group training"], "exact_pricing_wording": ""},
    "audience": {"ideal_member": "Busy adults", "prior_struggles": ""},
    "proof": {"wins": [], "verifiable_numbers": []},
    "media_notes": "Real members only",
    "approver": {"name": "Test Approver", "role": "Owner", "cell": "+13175550199", "email": "test@example.com"}
  }'
# Expected 200:
# {
#   "status": "received",
#   "account_key": "test",
#   "pending_source_count": <integer>,
#   "upload_url": "https://echo-intake-web-production.up.railway.app/u/testtoken-portalhandoff-2026"
# }
# Verify: status == "received", account_key == "test", upload_url ends with /u/testtoken-portalhandoff-2026
```

---

## 3. Verify PENDING sources landed in R2

This step requires R2 access (Blake only). In the R2 bucket, check:
```
intake/test/incoming/<stamp>_intake.json
```

Contents should have `"kind": "intake_form"`, `"source": "portal"`, `"client": "test"`, and `"answers"` with the fields from the POST above.

---

## 4. Upload one file

```bash
# Create a minimal test image (1x1 white JPEG)
python3 -c "
import io, struct
# Minimal valid JPEG
data = bytes.fromhex('ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffdb00430109090c0b0c180d0d1832211c2132323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc40000ffc4001f0100030101010101010101010000000000000102030405060708090a0bffc40000ffda0008010100003f00ffd9')
open('/tmp/test_upload.jpg', 'wb').write(data)
print('test image written')
"
# Upload it
curl -s -X POST \
  https://echo-intake-web-production.up.railway.app/u/testtoken-portalhandoff-2026 \
  -F "media=@/tmp/test_upload.jpg;type=image/jpeg" \
  -F "note=Integration test upload"
# Expected: HTML page containing "Got it."
# Verify the response body contains "Got it" (not an error page)
```

---

## 5. Verify tenant isolation (bad token returns 404)

```bash
curl -s -o /dev/null -w "%{http_code}" \
  https://echo-intake-web-production.up.railway.app/u/badtoken-notreal-999
# Expected: 404
# Verify: exactly 404, not 200 or 500

curl -s -o /dev/null -w "%{http_code}" \
  https://echo-intake-web-production.up.railway.app/intake/badtoken-notreal-999
# Expected: 404
```

---

## 6. Confirm nothing published

No publish can occur from this test:
- `AGENT_PUBLISH_ENABLED` is not set to `true` (it defaults `false`).
- The test account `test` has no Meta tokens.
- Intake submissions land in R2 as PENDING; the listener must explicitly process them and a human must approve any resulting draft.

---

## Cleanup

After the test: remove `AGENT_INTAKE_TOKEN_TEST` from Railway env. The test payload in R2 may be left or deleted; it will not cause any drafts to appear (the listener only processes R2 drops when `AGENT_INTAKE_ENABLED=true` and the intake ingest runs, which requires the listener service to process it).
