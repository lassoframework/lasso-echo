"""Default-OFF canonical generated publisher lease, never an intake bridge.

REST RPC transactions commit before provider I/O. Lost begin/outcome responses
retain a durable fence; no lease expires and no runtime reconciliation is offered.
Legacy reservations without owner-pinned canonical authority remain blocked.
"""
import uuid

from .generated_infographic_runtime import PREFIX, RuntimeHold, enabled


def rpc(store, name, payload):
    try:
        response = store._client().post(store._rest('rpc/' + name + '_20261007'),
            headers=store._headers({'Content-Type': 'application/json'}),
            json=payload, timeout=30)
        value = response.json()
        if not 200 <= response.status_code < 300 or not isinstance(value, dict):
            raise ValueError()
        return value
    except Exception:
        raise RuntimeHold('generated_send_transaction_uncertain') from None


def pins(value, tenant):
    expected = {'tenant_id', 'epoch', 'source_id', 'source_revision', 'palette_key', 'palette_revision'}
    if (not isinstance(value, dict) or set(value) != expected or value['tenant_id'] != tenant
            or any(type(value[k]) is not int or value[k] < 1 for k in
                   ('epoch', 'source_revision', 'palette_revision'))
            or any(not isinstance(value[k], str) or not value[k].strip() for k in
                   ('source_id', 'palette_key'))):
        raise RuntimeHold('generated_canonical_pins_required')
    return value


class GeneratedSendLease:
    def __init__(self, store, row, claim_token):
        if not enabled():
            raise RuntimeHold('generated_runtime_disabled')
        self.store = store
        self.row = row
        self.claim = str(uuid.UUID(claim_token))
        self.attempt = str(uuid.uuid5(uuid.NAMESPACE_URL, 'echo-generated-send:' + self.claim))
        self.begun = False
        self.begin_uncertain = False
        self.reserved = False
        self.provider = None

    def acquire(self):
        from .generated_infographic_runtime import _sql_publish_readback
        binding = _sql_publish_readback(self.store, self.row['id'])
        canonical = pins(binding.get('authority_pins') if isinstance(binding, dict) else None,
                         self.row['gym_id'])
        if self.row.get('generated_authority_pins') != canonical:
            raise RuntimeHold('generated_approval_pins_changed')
        value = rpc(self.store, 'generated_send_acquire', dict(p_attempt=self.attempt,
            p_tenant=self.row['gym_id'], p_row=self.row['id'], p_claim=self.claim,
            p_job=self.row['source_media_asset_id'][len(PREFIX):], p_pins=canonical))
        if (value.get('attempt_token') != self.attempt or value.get('state') != 'reserved'
                or value.get('replayed') is not False or value.get('authorize_send') is not False):
            raise RuntimeHold('generated_send_requires_reconciliation')
        self.reserved = True

    def begin(self, provider):
        if self.begun:
            # Meta has multiple required mutations within one invocation. The
            # forward context still binds each mutation to the same row/target.
            if provider != self.provider:
                raise RuntimeHold('generated_provider_changed')
            return
        self.begin_uncertain = True
        value = rpc(self.store, 'generated_send_begin', {'p_attempt': self.attempt})
        if (value.get('attempt_token') != self.attempt or value.get('state') != 'inflight'
                or value.get('authorize_send') is not True):
            raise RuntimeHold('generated_send_requires_reconciliation')
        self.begun, self.begin_uncertain, self.provider = True, False, provider

    def finish(self, *, result=None, error=None):
        if not self.reserved or self.begin_uncertain:
            # A lost begin response may already be inflight; no fabricated
            # cancellation or automatic second begin can clear that fence.
            raise RuntimeHold('generated_send_requires_reconciliation')
        post_id = str(getattr(result, 'media_id', '') or '').strip()
        if getattr(result, 'mode', '') == 'published' and not self.begun:
            # A local/provider cache result cannot certify this new canonical
            # attempt. Retain the reservation for independent reconciliation.
            raise RuntimeHold('generated_cached_result_requires_reconciliation')
        if error is None and getattr(result, 'ok', False) is True and getattr(result, 'mode', '') == 'published' and post_id and self.begun:
            outcome, receipt = 'sent', self.provider + ':post:' + post_id
        elif ((not self.begun) or getattr(error, 'definitive_no_post', False) is True
              or getattr(result, 'mode', '') == 'would_publish'
              or getattr(result, 'definitive_no_post', False) is True):
            outcome, receipt = 'not_sent', 'runtime:no-send:' + self.attempt
        else:
            outcome, receipt = 'unknown', 'runtime:unknown:' + self.attempt
        value = rpc(self.store, 'generated_send_outcome', dict(p_attempt=self.attempt,
            p_outcome=outcome, p_evidence={'actor': 'echo:generated-publisher', 'receipt_ref': receipt},
            p_reconcile=False))
        if value.get('attempt_token') != self.attempt or value.get('state') != outcome:
            raise RuntimeHold('generated_send_requires_reconciliation')
        return outcome
