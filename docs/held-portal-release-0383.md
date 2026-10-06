# Explicit held portal completion

`agent.held_portal_release.deliver_held_portal_notice` is an operator-invoked helper for an exact, independently verified, routine held Echo website-tab ticket with no Slack route. It has no scheduler or automatic call site. Ordinary outbox and self-service swap behavior are untouched.

Before use, independently inspect the actual repair, frozen revision and genuine HTTPS review and behavior receipts. Persist the exact verified evidence under the ticket's `verification_after.fixer.held_release_review` and use deployed `fixer_create_held_release_proof` to designate a new notice UUID and reviewed body. Proof creation and evidence persistence remain separate authorized operations. The helper accepts only the existing proof ID, ticket UUID and current request version. It cannot create evidence or a proof.

```python
from agent.held_portal_release import deliver_held_portal_notice
result = deliver_held_portal_notice(
    bus,
    ticket_id=exact_ticket_uuid,
    request_version=exact_current_version,
    proof_id=precreated_reviewed_proof_uuid,
)
```

The designated `ranger` status notice enters as `outbound/held`, excluded from ordinary ready outbox sweeps. Exact attachments include the 0383 release proof and full tenant, requester, version, predecessor and nullable Slack route fence. PostgreSQL rechecks the current proof under the ticket lock when the notice becomes `posted`; the portal's `clientVisible` filter and 0310 policy expose that posted status row to the ticket's client. This means the notice is available in the portal thread, not that the client has read it.

Only after exact posted-row readback does the helper call `fixer_release_held_delivery`. It reports `resolved: true` only after rereading the resolved current ticket and consumed exact proof. A lost release CAS reports `delivered: true, resolved: false`. A transport error propagates; rerun with the same ticket, version and proof after readback, never a fresh notice or synthetic receipt. A consumed proof resumes only if its exact immutable notice and resolved ticket still match. Changed identity, version, route, evidence or notice is rejected.

No posted notice metadata is updated. This preserves deployed 0383 and draft 0384 immutability and avoids the ordinary outbox's finalization metadata conflict. The 0384 draft's held resolution trigger accepts and consumes the same 0383 proof; the local PostgreSQL fixture exercises both migration states.

Validation uses synthetic data in a disposable local database. No production proof, notice, ticket transition, Slack delivery or merge was performed. Independent review and the task's release/production behavior gates are required before use. This helper does not establish that the ordinary photo swap path is repaired, and a completion body must describe only the complaint and repair supported by the actual receipts.
