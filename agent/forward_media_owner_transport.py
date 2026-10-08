"""Dedicated-owner DRAFT transport. Production runtime remains unprovisioned.

Reserve quarantine, read snapshot, end read tx, verify remote bytes, then lock
exact calendar, observation, asset/source and progress for one final transaction.
Source/history holds do not freeze a unique original clearance. Only
normal verified COMMIT reports success. Once reservation COMMIT is acknowledged,
crashes/exceptions/unknown final commits leave final outcome or quarantine;
discovery excludes both forever. Unknown initial reservation COMMIT stops before
any authority or external object reads begin. A fresh worker may only proceed
if that initial reservation actually aborted: the unique key blocks it while
the first transaction is unresolved, and excludes it if the first committed.
This is safe fresh admission, never replay of an attempted authority write.
The originating uncertain transport remains halted; no reconnect retry exists.
Manual independent reconciliation is required; there is no retry/expiry API.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
from types import MappingProxyType
import uuid

from . import forward_media_owner as owner
from .forward_media_owner_worker import OwnerTransport, OwnerWorkerHold, _identity

PREFIX = 'fixer_forward_media_owner_'


@dataclass(frozen=True)
class FrozenObjectReader(owner.ObjectReader):
    """Closed exact-byte map; missing URLs raise, never fall back to network."""
    _objects: object

    def __init__(self, objects):
        from .forward_media_prepare import MAX_OBJECT_LENGTH
        if (not isinstance(objects, dict) or not 1 <= len(objects) <= 3
                or any(not isinstance(url, str) or not isinstance(data, bytes)
                       or not 0 < len(data) <= MAX_OBJECT_LENGTH for url, data in objects.items())):
            raise OwnerWorkerHold('verified_local_byte_cache_required')
        object.__setattr__(self, '_objects', MappingProxyType(dict(objects)))

    def read(self, url):
        try:
            return self._objects[url]
        except KeyError:
            raise OwnerWorkerHold('verified_local_byte_cache_required') from None


class DedicatedOwnerTransport(OwnerTransport):
    """Construct only with the same dedicated owner persistence/connection.

    No credentials/factory fallback and no callable producer history verifier.
    The separately applied source/history draft enables independent original
    receipts; unknown history stays held with no per-asset clearance insertion.
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
        self._final_phase = False

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
    def reserved_current(self, candidate):
        """Durable reservation then lock-free snapshot; no open tx at yield.

        Remote failures are recorded only in a subsequent final transaction.
        Crash/exception before final outcome retains quarantine permanently.
        """
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
            current = self._rpc('snapshot', self._active)
            self._conn.rollback()  # End read tx BEFORE any remote byte I/O.
            yield current
            if not self._recorded:
                raise OwnerWorkerHold('durable_progress_commit_unverified')
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._active = None
            self._recorded = False

    @contextmanager
    def locked_current(self, candidate):
        """Final graph-before-row revalidation, staging and acknowledged COMMIT.

        Direct reservation is retained for transaction mechanics callers; the
        concrete worker always enters reserved_current for remote preparation.
        This context must never execute remote readers or render replay.
        """
        if self._broken or self._final_phase:
            raise OwnerWorkerHold('owner_manual_reconciliation_required')
        self._require_idle()
        key = _identity(candidate)
        direct = self._active is None
        try:
            owner.check_environment()
            self.persistence._assert_owner_identity()
            if direct:
                token = str(uuid.uuid4())
                reserved = self._rpc('reserve', (*key, token))
                self._commit()
                if reserved is not True:
                    raise OwnerWorkerHold('owner_manual_reconciliation_required')
                self._active = (*key, token)
                self._recorded = False
            elif key != self._active[:3]:
                raise OwnerWorkerHold('owner_transaction_contract_required')
            with self._conn.cursor() as cur:
                cur.execute("set local lock_timeout='5s'; set local statement_timeout='15s'")
            self._final_phase = True
            current = self._rpc('locked', self._active)
            yield current
            if not self._recorded:
                raise OwnerWorkerHold('durable_progress_commit_unverified')
            self._commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._final_phase = False
            if direct:
                self._active = None
                self._recorded = False

    def stage_authority(self, candidate, persistence, tuples, *, local_reader=None):
        if (persistence is not self.persistence or self._active is None
                or not self._final_phase or _identity(candidate) != self._active[:3]):
            raise OwnerWorkerHold('owner_transaction_contract_required')
        if type(local_reader) is not FrozenObjectReader:
            raise OwnerWorkerHold('verified_local_byte_cache_required')
        # Persistence's defensive rereads are strictly local in the final phase.
        previous = persistence._reader
        try:
            persistence._reader = local_reader
            return persistence.persist_in_transaction(*tuples)
        finally:
            persistence._reader = previous

    def verified_history(self, original):
        # No independent byte-audited fleet history contract is provisioned.
        # Producer observations, used_count and caller booleans cannot clear it.
        raise OwnerWorkerHold('verified_history_transport_missing')

    def record(self, candidate, outcome):
        if self._active is None or not self._final_phase or _identity(candidate) != self._active[:3]:
            raise OwnerWorkerHold('owner_transaction_contract_required')
        result = self._rpc('record', (*self._active, json.dumps(outcome)))
        if result is not True:
            raise OwnerWorkerHold('durable_progress_commit_unverified')
        self._recorded = True
        return True  # Staged, not durable until locked_current exits normally.
