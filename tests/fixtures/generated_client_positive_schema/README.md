# Disposable positive rehearsal schema prerequisite

Construction only. The harness reuses the accepted release installer's explicitly
synthetic base tables and its actual source extracts, then executes the full
owner/source-brand, inventory/census, calendar reservation, generated admission,
prepare, service preparation, stage/status and issuer dispatch migrations.
`frozen_inputs.json` pins all 48 files read by that installer and its source
extractors. This is a composed test schema, not a production schema restore.

Run from the repository with the existing psycopg/pytest environment:

```
PG17_BIN=/opt/homebrew/Cellar/postgresql@17/17.11/bin PYTHONPATH=/tmp/echo-0611-test-deps python3 -m pytest tests/test_generated_client_positive_schema_pg.py -q
```

Observed October 9, 2026: 1 passed in 1.58 seconds. Five distinct restricted
LOGIN principals are provisioned only in the temporary Unix-socket-only PG17
cluster. Owner, reader, producer and service each have one authority membership;
issuer has only the two required dispatch and hosted-byte issuer memberships.
Tenant authorization succeeds for the fixture tenant and refuses a foreign one.
Principal/control dictionaries cannot be read by those identities. Controls
remain disabled and calendar, artifact, receipt, admission, stage and dispatch
request tables remain empty. The cluster is stopped and its temporary directory
removed on exit. No provider, storage, production DSN, credential, or /data mount
is read or changed.

Remaining prerequisites for positive full-chain proof: an isolated Linux runtime
with an actual durable /data mount, shared journal, synthetic source/calendar and
inventory authority rows, and bounded fake external generation/review/storage/
public-byte transports. Actual CLI environment, constructors, SQL and journal
guards must stay intact. This construction result does not prove positive
prepare, stage/resume, activation, approval, finalization or publishing.
