"""Mapped non-demo accounts for explicitly armed fleet video creation."""
import re
import time
from .story_studio_store import SupabaseStoryStudioStore
from . import echo_clients

_cache = None

def load(*, store=None):
    st=store or SupabaseStoryStudioStore()
    if not st.available():
        raise ValueError('Automatic reel account roster is unavailable')
    clients = echo_clients.snapshot(fresh=True)
    if not clients.ok:
        raise ValueError('Echo client enrollment is unavailable')
    gyms=st._get_all('gyms',{'select':'id,is_demo'})
    allowed={r['id'] for r in gyms if r.get('is_demo') is not True and r.get('id') and clients.is_client(r['id'])}
    rows=st._get_all('echo_intake_tokens',{'select':'gym_id,echo_account_key'})
    keys=set()
    for row in rows:
        key=str(row.get('echo_account_key') or '').strip().lower()
        if row.get('gym_id') in allowed and re.fullmatch(r'[a-z0-9][a-z0-9_]{0,100}',key):
            for suffix in ('_ig','_fb'):
                if key.endswith(suffix):key=key[:-len(suffix)];break
            keys.add(key)
    return sorted(keys)

def gyms():
    global _cache
    now=time.monotonic()
    if _cache and now-_cache[0]<300:
        return list(_cache[1])
    # Never renew a stale cache after a failed read.
    keys=load()
    _cache=(now,keys)
    return list(keys)
