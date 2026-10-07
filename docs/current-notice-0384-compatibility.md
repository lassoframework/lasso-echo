# Echo pre 0384 compatibility mode

`AGENT_FIXER_CURRENT_NOTICE_0384` defaults to disabled. Only an explicit value of
`true` enables the portal 0384 reservation, route binding and exact notice resolver
RPCs. In disabled mode the bus refuses those operations locally before any RPC
request. A missing RPC response is never used as feature detection or as a reason
to fall back to an older completion notice.

For Echo website tab support tickets, compatibility mode blocks completion row
creation, first contact completion outreach before opening a DM, queued completion
dispatch, human resolve taps and posted notice finalization. Existing queued rows
remain queued and their tickets remain open. Both tokenized and older unreserved
completion rows are covered. No completion receipt or resolved success is produced
by these blocked paths. Safe inbound intake, acknowledgements and internal
escalations retain their existing gates and worker leases. The separately reviewed
0383 explicit held release helper retains its proof protocol.

Deploy this reviewed caller with the flag absent or false before the separately
authorized 0384 migration. Record the actual deployed SHA and verify disabled
behavior independently. Keep the flag disabled until the portal owner establishes
0381 through 0384 applied state, queue disposition, sender lease exclusion and
exact current notice smoke evidence under the release checklist. Enabling the flag
is a separate authorized release action. This code and its synthetic tests do not
establish any production deployment, migration, ticket resolution or client send.
