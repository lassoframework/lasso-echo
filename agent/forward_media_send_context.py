"""Default-OFF, one-send authority scope for lower publisher boundaries.

Only authorized_send performs the trusted bridge/receipt checks and creates a
scope. A boolean, draft attribute, URL, local publisher claim or another task's
context cannot grant it. Callers must explicitly wrap the actual provider send;
plain forward_media_publish.authorize never creates ambient permission.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import re
import threading
import time

from . import forward_media_guard as guard

_SCOPE=ContextVar('forward_media_send_scope',default=None)
_CALL=ContextVar('forward_media_provider_call',default=None)
_PLATFORM={'instagram':('instagram','_ig'),'facebook':('facebook_page','_fb')}
_VIDEO=re.compile(r'\.(mp4|mov|m4v|webm|avi)(?:[?#]|$)',re.I)


class ProviderSendHold(guard.ForwardMediaVerificationHold):
    def __init__(self,reason,*,attempted=False):
        super().__init__(reason)
        self.definitive_no_post=not attempted


class ProviderDuplicateHold(guard.ForwardMediaDuplicateHold):
    def __init__(self,*,attempted=False):
        super().__init__('media bytes were already consumed')
        self.definitive_no_post=not attempted


def _actor():
    try: task=asyncio.current_task()
    except RuntimeError: task=None
    return threading.get_ident(),id(task) if task is not None else None


class _Lease:
    def __init__(self,store,row,token,image_receipt):
        self.store,self.row,self.token=store,row,token
        self.image_receipt=image_receipt
        self.actor=_actor()
        self.deadline=time.monotonic()+120
        self.closed=False
        self.used=False
        self.attempted=False
        self.lock=threading.Lock()

    def require(self):
        if self.closed or self.actor!=_actor() or time.monotonic()>self.deadline:
            raise ProviderSendHold('current provider authority scope unavailable',attempted=self.attempted)


def _read_one(store,table,params):
    response=store._client().get(store._rest(table),params=dict(params,limit='2'),
                                headers=store._headers(),timeout=30)
    rows=response.json()
    if (not 200<=response.status_code<300 or not isinstance(rows,list)
            or len(rows)!=1 or not isinstance(rows[0],dict)):
        raise ProviderSendHold('committed provider authority receipt unavailable')
    return rows[0]


@contextmanager
def authorized_send(store,row,claim_token):
    """Explicit trusted bridge scope; always closes even on exceptions.

    No credentials/factory fallback. Once scope is used it cannot authorize a
    second publish; thread/task inheritance cannot reuse it. OFF yields without
    touching store, row, receipts or publisher state.
    """
    if not guard.enabled():
        yield
        return
    from . import forward_media_publish as bridge
    lease=None
    context_token=None
    try:
        frozen=json.loads(json.dumps(row,allow_nan=False))
        if not isinstance(frozen,dict) or frozen.get('account') not in _PLATFORM:
            raise ProviderSendHold('scoped IG or FB calendar authority required')
        token=guard._uuid(claim_token)
        if bridge.authorize(store,frozen,token) is not True:
            raise ProviderSendHold('atomic provider authority unavailable')
        store=getattr(store,'_s',store)
        receipt=_read_one(store,'fixer_forward_media_claim_receipt_20261006',
            {'claim_token':'eq.'+token,'calendar_row_id':'eq.'+guard._uuid(frozen['id']),
             'select':'*'})
        if (str(receipt.get('claim_token'))!=token
                or str(receipt.get('calendar_row_id'))!=frozen['id']
                or receipt.get('post_date')!=frozen.get('post_date')
                or receipt.get('group_key')!=frozen.get('visual_group_key')
                or receipt.get('source_url')!=frozen.get('source_media_url')
                or receipt.get('image_url')!=frozen.get('image_url')
                or receipt.get('thumbnail_url')!=frozen.get('thumbnail_url')
                or not isinstance(receipt.get('tenant_id'),str) or not receipt['tenant_id']):
            raise ProviderSendHold('committed provider receipt differs from outgoing row')
        image=_read_one(store,'fixer_forward_media_object_read_20261006',
            {'tenant_id':'eq.'+receipt['tenant_id'],'exact_url':'eq.'+frozen['image_url'],
             'select':'tenant_id,exact_url,fingerprint,byte_length'})
        if (image.get('tenant_id')!=receipt['tenant_id'] or image.get('exact_url')!=frozen['image_url']
                or not re.fullmatch(r'md5:[0-9a-f]{32}',str(image.get('fingerprint','')))
                or type(image.get('byte_length')) is not int or not 0<image['byte_length']<=134217728
                or image['fingerprint'] not in receipt.get('fingerprints',[])):
            raise ProviderSendHold('trusted delivered byte receipt unavailable')
        lease=_Lease(store,frozen,token,image)
        context_token=_SCOPE.set(lease)
        yield
    except ProviderSendHold:
        raise
    except guard.ForwardMediaDuplicateHold:
        raise ProviderDuplicateHold(attempted=bool(lease and lease.attempted)) from None
    except guard.ForwardMediaVerificationHold as exc:
        raise ProviderSendHold('provider scope verification failed',attempted=bool(lease and lease.attempted)) from None
    except Exception:
        # Preserve provider/runtime failures from INSIDE the actual send. Their
        # ambiguity and existing claim retention are owned by the publisher.
        if context_token is not None:
            raise
        raise ProviderSendHold('provider scope verification unavailable') from None
    finally:
        if lease is not None: lease.closed=True
        if context_token is not None: _SCOPE.reset(context_token)


def _validate_draft(lease,draft,account):
    lease.require()
    row=lease.row
    expected_platform,suffix=_PLATFORM[row['account']]
    key=getattr(account,'key',None)
    expected_key=str(row.get('gym_id') or '')+suffix
    if key!=expected_key:
        # UUID-backed gyms need a CURRENT persisted alias resolution, never a
        # suffix guess or the normal six-hour registry cache.
        resolver=getattr(lease.store,'_resolve_gym_uuid_uncached',None)
        if (not isinstance(key,str) or not key.endswith(suffix) or not callable(resolver)
                or str(resolver(key[:-len(suffix)]))!=str(row.get('gym_id'))):
            raise ProviderSendHold('provider account differs from authorized gym',attempted=lease.attempted)
    fmt=row.get('format')
    if (str(getattr(draft,'draft_id',''))!=row['id']
            or getattr(draft,'account_key',None)!=key
            or getattr(account,'platform',None)!=expected_platform
            or getattr(draft,'platform',None)!=expected_platform
            or getattr(draft,'day_key',None)!=row.get('post_date')
            or getattr(draft,'creative_public_url',None)!=row.get('image_url')
            or getattr(draft,'caption',None)!=row.get('caption')
            or fmt not in ('feed','story') or bool(getattr(draft,'is_story',False))!=(fmt=='story')
            or getattr(draft,'slide_urls',None)
            or _VIDEO.search(str(getattr(draft,'creative_public_url','')))
            or _VIDEO.search(str(getattr(draft,'creative_path','')))):
        raise ProviderSendHold('outgoing provider creative differs from authorized row',attempted=lease.attempted)
    for field in ('gym_id','visual_group_key','publish_claim_token'):
        value=getattr(draft,field,None)
        expected=lease.token if field=='publish_claim_token' else row.get(field)
        if value not in (None,'') and value!=expected:
            raise ProviderSendHold('outgoing provider identity differs from authority',attempted=lease.attempted)


def _revalidate(lease):
    from . import forward_media_publish as bridge
    try:
        if bridge.authorize(lease.store,lease.row,lease.token) is not True:
            raise ProviderSendHold('current atomic provider authority unavailable',attempted=lease.attempted)
    except guard.ForwardMediaDuplicateHold:
        raise ProviderDuplicateHold(attempted=lease.attempted) from None
    except Exception:
        raise ProviderSendHold('current atomic provider authority unavailable',attempted=lease.attempted) from None


def guarded_publisher(provider):
    def decorate(fn):
        @wraps(fn)
        def wrapped(draft,account,*args,**kwargs):
            if not guard.enabled(): return fn(draft,account,*args,**kwargs)
            from . import config
            if (not config.publish_enabled() or (provider=='zernio' and not config.zernio_publish_enabled())
                    or (getattr(draft,'is_story',False) and not config.stories_enabled())):
                return fn(draft,account,*args,**kwargs)
            lease=_SCOPE.get()
            if type(lease) is not _Lease:
                raise ProviderSendHold('current provider authority scope required')
            _validate_draft(lease,draft,account)
            with lease.lock:
                lease.require()
                if lease.used: raise ProviderSendHold('provider authority scope already used',attempted=lease.attempted)
                lease.used=True
            _revalidate(lease)
            target=(account.get_target_id() if provider=='meta' and callable(getattr(account,'get_target_id',None)) else None)
            token=_CALL.set((lease,provider,str(target) if target is not None else None))
            try: return fn(draft,account,*args,**kwargs)
            finally: _CALL.reset(token)
        return wrapped
    return decorate


def boundary(provider,*,draft=None,account=None,format=None,target=None,attempt=False):
    if not guard.enabled(): return
    active=_CALL.get()
    if not isinstance(active,tuple) or len(active)!=3 or active[1]!=provider or type(active[0]) is not _Lease:
        raise ProviderSendHold('verified lower provider call required')
    lease=active[0]; lease.require()
    if draft is not None and account is not None: _validate_draft(lease,draft,account)
    if format is not None and lease.row.get('format')!=format:
        raise ProviderSendHold('separate format requires its own owned authority',attempted=lease.attempted)
    if target is not None and str(target)!=active[2]:
        raise ProviderSendHold('provider target changed after authorization',attempted=lease.attempted)
    if attempt:
        # Repeat the atomic row/token/revision check immediately before every
        # external mutation, including Meta's post-poll media_publish step.
        # No database lock is retained across any provider network request.
        _revalidate(lease)
        lease.require()
        lease.attempted=True


def delivered_bytes(data,url):
    if not guard.enabled(): return
    boundary('socialapi')
    lease=_CALL.get()[0]
    receipt=lease.image_receipt
    if (url!=receipt['exact_url'] or not isinstance(data,bytes)
            or len(data)!=receipt['byte_length']
            or 'md5:'+hashlib.md5(data).hexdigest()!=receipt['fingerprint']):
        raise ProviderSendHold('uploaded bytes differ from trusted delivered object',attempted=lease.attempted)


def unsupported(kind):
    if guard.enabled():
        active=_CALL.get()
        raise ProviderSendHold('separate '+kind+' authority required',
                               attempted=bool(active and active[0].attempted))


def current_claim_token(provider):
    boundary(provider)
    return _CALL.get()[0].token
