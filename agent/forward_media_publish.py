"""Publisher bridge to persisted trusted evidence; never runs the attester."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import forward_media_guard as guard

# Outgoing creative must still match the leased persisted row. Authority also
# validates the attestation revision atomically inside its claim transaction.
_CREATIVE = ('id', 'gym_id', 'account', 'format', 'post_date', 'caption',
             'image_url', 'source_media_url', 'thumbnail_url', 'visual_group_key',
             'gbp_topic_type', 'gbp_cta_type', 'gbp_cta_url', 'gbp_event', 'gbp_offer',
             'gbp_location_id', 'pillar')


def _bind_gbp_reservation_day(store, row, token, persisted):
    """Bind the actual gym-local attempt day under this unsent GBP lease."""
    if row.get('account') != 'googlebusiness':
        return persisted
    try:
        from . import config
        zone = ZoneInfo(config.posting_timezone_for(row['gym_id']))
        day = datetime.now(timezone.utc).astimezone(zone).date().isoformat()
    except Exception as exc:
        raise guard.ForwardMediaVerificationHold('gym-local send day unavailable') from exc
    current = persisted.get('publish_reservation_day')
    if current is not None:
        if str(current) != day:
            raise guard.ForwardMediaVerificationHold('owned GBP reservation day is stale')
        return persisted
    try:
        response = store._client().patch(
            store._rest('content_calendar'),
            params={'id': 'eq.' + guard._uuid(row['id']),
                    'status': 'eq.publishing',
                    'publish_claim_token': 'eq.' + token,
                    'publish_reservation_day': 'is.null'},
            headers=store._headers({'Content-Type': 'application/json',
                                    'Prefer': 'return=representation'}),
            json={'publish_reservation_day': day}, timeout=30)
        rows = response.json()
    except Exception as exc:
        raise guard.ForwardMediaVerificationHold('owned GBP reservation day unavailable') from exc
    if (not 200 <= response.status_code < 300 or not isinstance(rows, list)
            or len(rows) != 1 or not isinstance(rows[0], dict)
            or str(rows[0].get('id')) != str(row['id'])
            or str(rows[0].get('publish_claim_token') or '') != token
            or rows[0].get('status') != 'publishing'
            or str(rows[0].get('publish_reservation_day') or '') != day):
        raise guard.ForwardMediaVerificationHold('owned GBP reservation day unavailable')
    return rows[0]


def authorize(store, row, claim_token):
    """Require owned persisted row, persisted evidence and literal-true authority."""
    if not guard.enabled():
        return True
    try:
        # GBP owns a facade around the same calendar REST authority.
        store = getattr(store, '_s', store)
        row_id = guard._uuid(row.get('id'))
        token = guard._uuid(claim_token)
        persisted = store.get_row(row.get('gym_id'), row_id)
        if (not isinstance(persisted, dict)
                or persisted.get('status') != 'publishing'
                or str(persisted.get('publish_claim_token') or '') != token
                or any(persisted.get(key) != row.get(key) for key in _CREATIVE)):
            raise guard.ForwardMediaVerificationHold('owned persisted creative unavailable or changed')
        from .generated_infographic_runtime import validate_publish_palette, RuntimeHold
        try:
            validate_publish_palette(persisted, store=store)
        except RuntimeHold as exc:
            raise guard.ForwardMediaVerificationHold(str(exc)) from None
        persisted = _bind_gbp_reservation_day(store, row, token, persisted)
        if any(persisted.get(key) != row.get(key) for key in _CREATIVE):
            raise guard.ForwardMediaVerificationHold('owned GBP creative changed while binding send day')
        snapshot_response = store._client().post(
            store._rest('rpc/fixer_forward_media_attestation_request_20261006'),
            headers=store._headers({'Content-Type': 'application/json'}),
            json={'p_calendar_row_id': row_id}, timeout=30)
        snapshot = snapshot_response.json()
        expected = {'calendar_row_id': row_id,
                    'gym_id': row.get('gym_id'), 'account': row.get('account'),
                    'format': row.get('format'),
                    'gbp_location_id': row.get('gbp_location_id'),
                    'post_date': row.get('post_date'),
                    'group_key': row.get('visual_group_key'),
                    'source_url': row.get('source_media_url'),
                    'image_url': row.get('image_url'), 'thumbnail_url': row.get('thumbnail_url')}
        if (not 200 <= snapshot_response.status_code < 300 or not isinstance(snapshot, dict)
                or any(snapshot.get(key) != value for key, value in expected.items())
                or not isinstance(snapshot.get('revision'), str) or not snapshot['revision']):
            raise guard.ForwardMediaVerificationHold('outgoing media snapshot unavailable or changed')
        revision = snapshot['revision']
        response = store._client().get(
            store._rest('fixer_forward_media_lineage_20261006'),
            params={'calendar_row_id': 'eq.' + row_id,
                    'row_revision': 'eq.' + revision,
                    'select': 'evidence_id', 'order': 'verified_at.desc', 'limit': '1'},
            headers=store._headers(), timeout=30)
        evidence = response.json()
        if (not 200 <= response.status_code < 300 or not isinstance(evidence, list)
                or len(evidence) != 1 or not isinstance(evidence[0], dict)):
            raise guard.ForwardMediaVerificationHold('persisted trusted media evidence unavailable')
        evidence_id = guard._uuid(evidence[0].get('evidence_id'))
        if guard.claim(store, row_id, token, evidence_id, revision) is not True:
            raise guard.ForwardMediaVerificationHold('atomic media claim did not return literal true')
        return True
    except guard.ForwardMediaVerificationHold:
        raise
    except Exception as exc:
        raise guard.ForwardMediaVerificationHold('persisted forward media authority unavailable') from exc


def hold_result(store, row, claim_token):
    """GBP result shape keeps pre-network holds separate from ambiguous sends."""
    try:
        authorize(store, row, claim_token)
    except guard.ForwardMediaVerificationHold as exc:
        reason = ('forward_media_duplicate' if isinstance(exc, guard.ForwardMediaDuplicateHold)
                  else 'forward_media_verification')
        return {'ok': False, 'status': 'approved', 'late_post_id': '',
                'reject_reason': reason + ': ' + str(exc), 'held': reason, 'mode': ''}
    return None


def authorized_send(store, row, claim_token):
    """Explicit one-send scope around the actual lower publisher invocation.

    Plain authorize() remains a verification operation and grants no ambient
    lower-publisher permission. Calendar callers must adopt this explicit scope
    before activation; direct approval/chat callers without it hold safely.
    """
    from .forward_media_send_context import authorized_send as scope
    return scope(store, row, claim_token)
