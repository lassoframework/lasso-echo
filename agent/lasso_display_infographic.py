"""Bounded background rendering for missing LASSO portal display images.

Never generates a paid image synchronously inside a calendar GET. A repeated
request joins the same pending job and reads the reviewed hosted result later.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from pathlib import Path
import hashlib
import json
import tempfile
import time

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='lasso-display')
_lock = Lock()
_jobs = {}


def _render(post, tenant, key):
    from . import creative_studio, media_host
    from .lasso_infographic_content import select_copy
    copy = select_copy(str(post.get('caption') or post.get('pillar') or ''))
    story = 'story' in str(post.get('format') or '').lower()
    with tempfile.TemporaryDirectory(prefix='lasso-astra-display-') as folder:
        result = creative_studio.generate(
            copy['headline'], copy['facts'], cta=copy['cta'], footer=copy.get('footer'), account_key=tenant,
            surface='story' if story else 'feed post',
            out_path=str(Path(folder)/'candidate.png'),
            draft_id='display-' + str(post.get('id') or ''))
        if not result:
            return None
        url = media_host.host_media(result['path'], tenant)
        if not url:
            return None
        from .infographic_artifacts import ArtifactStore
        ArtifactStore().save(tenant, url, result['path'],
            {'source_id': copy['source_id'], 'source_hash': copy['source_hash']}, key)
        return url


def _render_and_persist(post, tenant, key):
    from . import db
    from .infographic_artifacts import ArtifactStore
    import uuid
    store = ArtifactStore()
    owner = str(uuid.uuid4())
    if not store.claim(tenant, key, owner):
        return None
    try:
        # Another worker may have completed between our initial read and claim.
        url = store.cached(tenant, key) or _render(post, tenant, key)
    except Exception:
        url = None
    finally:
        store.release(tenant, key, owner)
    db.kv_set(key, json.dumps({'url': url, 'retry_after': time.time() + 300}))
    return url


def display_image_for(post, tenant):
    from . import db
    brain = Path(__file__).resolve().parent.parent / 'brand_voice' / 'lasso_now.md'
    try:
        from . import config, summit
        from .infographic_evidence import POLICY_VERSION
        campaign = Path(config.KNOWLEDGE_DIR) / summit.SUMMIT_FILE
        brain_hash = hashlib.sha256(brain.read_bytes() +
            (campaign.read_bytes() if campaign.exists() else b'') +
            POLICY_VERSION.encode()).hexdigest()
        key = 'lasso_display_v1_' + hashlib.sha256(json.dumps(
            [tenant, post.get('id'), post.get('caption'), post.get('pillar'),
             post.get('format'), brain_hash], sort_keys=True).encode()).hexdigest()
        from .infographic_artifacts import ArtifactStore
        durable = ArtifactStore().cached(tenant, key)
        if durable:
            return durable
        saved = json.loads(db.kv_get(key, '{}'))
        if saved.get('retry_after', 0) > time.time():
            return None
    except Exception:
        return None
    with _lock:
        # Completed futures are persisted and removed; cache size cannot grow
        # forever or permanently prevent a later request after a transient error.
        for old_key, future in list(_jobs.items()):
            if not future.done():
                continue
            try:
                url = future.result()
            except Exception:
                url = None
            db.kv_set(old_key, json.dumps({'url': url, 'retry_after': time.time() + 300}))
            del _jobs[old_key]
            if old_key == key:
                return url
        if key not in _jobs and len(_jobs) < 2:
            _jobs[key] = _pool.submit(_render_and_persist, dict(post), tenant, key)
        return None
