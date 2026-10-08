"""DRAFT dedicated-owner source receipts and bounded trusted history transport.

No connection factory, production activation or automatic historical import.
Snapshot ends its own read transaction before remote I/O; stage/lookup leave
COMMIT to the owner transport. Every decision is hold_used/hold_uncertain:
old/current Drive reads and absence from a bounded index cannot clear history.

Positive-clearance handoff requirements (NOT implemented by this package):
1. Freeze candidate's authenticated original bytes/Drive version, same-gym
   source binding, exact hosted bytes and immutable source receipt.
2. Freeze the COMPLETE fleet send corpus, including published_at/late_post_id
   rows and every delivered URL/snapshot. The current verified census includes
   1,398 published rows and 1,050 distinct delivered URLs; 517/72 were narrower
   cohorts. Re-census when starting an audit, never treat these counts as a spec.
3. Independently review every reachable historic original or delivered visual
   against this candidate, record per-row disposition and evidence, and hold on
   any unreachable/ambiguous source/rendition. Visual review is an explicit
   candidate-specific owner ruling, not cryptographic proof of original nonuse.
4. Preserve candidate/corpus digests, reviewer identity, raw byte evidence and
   stated visual uncertainty in a durable audit receipt. Revalidate corpus and
   source binding at commit, with a reviewed policy for concurrent sends.
5. The lead must explicitly integrate that reviewed visual-ruling contract into
   the existing fresh-production-only authority rule; a preexisting Drive upload
   has no fresh-production proof and cannot silently inherit cleared_unused.
"""
import json

from .forward_media_source_verifier import SourceVerificationHold, VerifiedSource


class SourceHistoryStore:
    def __init__(self, persistence):
        from .forward_media_owner import ForwardMediaOwnerPersistence
        if not isinstance(persistence, ForwardMediaOwnerPersistence):
            raise SourceVerificationHold('dedicated_source_owner_required')
        self.persistence = persistence
        self._conn = persistence._conn

    def _rpc(self, name, values):
        with self._conn.cursor() as cur:
            cur.execute('select public.fixer_forward_media_' + name + '_20261007('
                        + ','.join(['%s'] * len(values)) + ')', values)
            return cur.fetchone()[0]

    def snapshot(self, calendar_row_id, revision):
        """Only call while idle, after durable reservation and before remote I/O."""
        from psycopg.pq import TransactionStatus
        if self._conn.info.transaction_status != TransactionStatus.IDLE:
            raise SourceVerificationHold('source_snapshot_requires_idle_transaction')
        try:
            self.persistence._assert_owner_identity()
            result = self._rpc('source_snapshot', (calendar_row_id, revision))
            return result
        finally:
            self._conn.rollback()

    def stage_source(self, verified):
        """Under final graph/row locks; rechecks exact binding before append."""
        if not isinstance(verified, VerifiedSource):
            raise SourceVerificationHold('verified_source_required')
        self.persistence._assert_owner_identity()
        e = verified.evidence
        result = self._rpc('source_record', (e['calendar_row_id'], e['row_revision'],
                            e['binding_revision'], verified.receipt_ref,
                            json.dumps(e, sort_keys=True, separators=(',', ':'), ensure_ascii=False)))
        if result is not True:
            raise SourceVerificationHold('source_receipt_staging_failed')
        return verified.original

    def history(self, original, limit=2500):
        """Final transaction lookup. Raw driver errors must abort final commit."""
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise SourceVerificationHold('history_bounds_invalid')
        self.persistence._assert_owner_identity()
        return self._rpc('source_history', (json.dumps(original.row()), limit))
