"""Dedicated-owner DRAFT transport. Unprovisioned production; no trusted history.

Reserve a permanent quarantine key before starting work. Lock exact calendar,
observation, asset and progress for authority+outcome in one transaction. Only
normal verified COMMIT reports success. Once reservation COMMIT is acknowledged,
crashes/exceptions/unknown final commits leave final outcome or quarantine;
discovery excludes both forever. Unknown initial reservation COMMIT stops before
any authority begins, but an uncommitted reservation cannot durably exclude a
fresh process. Fleet-wide quarantine of that first-write uncertainty remains a
release gap requiring a separate durable coordinator. No reconnect retry exists.
Manual independent reconciliation is required; there is no retry/expiry API.
"""
from contextlib import contextmanager
import json
import uuid

from . import forward_media_owner as owner
from .forward_media_owner_worker import OwnerTransport, OwnerWorkerHold, _identity

PREFIX = 'fixer_forward_media_owner_'


class DedicatedOwnerTransport(OwnerTransport):
    """Construct only with the same dedicated owner persistence/connection.

    No credentials/factory fallback and no callable producer history verifier.
    Actual media_asset lacks audited original source receipts; history holds.
    """
    atomic_authority_outcome = True

    def __init__(self, persistence):
        if not isinstance(persistence, owner.ForwardMediaOwnerPersistence):
            raise OwnerWorkerHold('owner_environment_invalid')
        self.persistence = persistence
        self._conn = persistence._conn
        if getattr(self._conn, 'autocommit', None) is not False:
            raise OwnerWorkerHold('owner_environment_invalid')
        self._active = None
        self._recorded = False
        self._broken = False

    def _require_idle(self):
        # Never commit caller-owned staged work with a quarantine reservation.
        from psycopg.pq import TransactionStatus
        if self._conn.info.transaction_status != TransactionStatus.IDLE:
            raise OwnerWorkerHold('owner_transaction_contract_required')

    def _rpc(self, operation, args):
        # Names are internal constants; never interpolate caller-controlled SQL.
        placeholders = ','.join(['%s'] * len(args))
        with self._conn.cursor() as cur:
            cur.execute(f'select public.{PREFIX}{operation}_20261007({placeholders})', args)
            result = cur.fetchone()
        if not result:
            raise OwnerWorkerHold('owner_transport_unavailable')
        return result[0]

    def _commit(self):
        try:
            self._conn.commit()
        except Exception:
            self._broken = True
            raise owner.UncertainCommitError('owner transport commit uncertain') from None

    def pending(self, tenants, limit):
        if self._broken or self._active is not None:
            raise OwnerWorkerHold('owner_manual_reconciliation_required')
        self._require_idle()
        owner.check_environment()
        self.persistence._assert_owner_identity()
        result = self._rpc('pending', (list(tenants), limit))
        self._conn.rollback()  # End read-only identity/discovery transaction.
        return result

    @contextmanager
    def locked_current(self, candidate):
        if self._broken or self._active is not None:
            raise OwnerWorkerHold('owner_manual_reconciliation_required')
        self._require_idle()
        key = _identity(candidate)
        token = str(uuid.uuid4())
        try:
            owner.check_environment()
            self.persistence._assert_owner_identity()
            reserved = self._rpc('reserve', (*key, token))
            self._commit()  # Durable quarantine BEFORE canonical/authority transaction.
            if reserved is not True:
                raise OwnerWorkerHold('owner_manual_reconciliation_required')
            self._active = (*key, token)
            self._recorded = False
            current = self._rpc('locked', self._active)
            yield current
            if not self._recorded:
                raise OwnerWorkerHold('durable_progress_commit_unverified')
            self._commit()  # Canonical locks span staged authority AND outcome.
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._active = None
            self._recorded = False

    def stage_authority(self, candidate, persistence, tuples):
        if (persistence is not self.persistence or self._active is None
                or _identity(candidate) != self._active[:3]):
            raise OwnerWorkerHold('owner_transaction_contract_required')
        return persistence.persist_in_transaction(*tuples)

    def verified_history(self, original):
        # No independent byte-audited fleet history contract is provisioned.
        # Producer observations, used_count and caller booleans cannot clear it.
        raise OwnerWorkerHold('verified_history_transport_missing')

    def record(self, candidate, outcome):
        if self._active is None or _identity(candidate) != self._active[:3]:
            raise OwnerWorkerHold('owner_transaction_contract_required')
        result = self._rpc('record', (*self._active, json.dumps(outcome)))
        if result is not True:
            raise OwnerWorkerHold('durable_progress_commit_unverified')
        self._recorded = True
        return True  # Staged, not durable until locked_current exits normally.
