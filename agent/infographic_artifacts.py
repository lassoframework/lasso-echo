"""Service-only shared provenance for hosted LASSO infographics."""
from . import config
from .infographic_evidence import reviewed_asset, POLICY_VERSION, brain_snapshot


class ArtifactStore:
    def __init__(self, http=None):
        if http is None:
            import requests
            http = requests
        self.http = http
        self.url = config.supabase_url().rstrip('/') + '/rest/v1/echo_infographic_artifacts'
        key = config.supabase_service_key()
        self.headers = {'apikey': key, 'Authorization': 'Bearer ' + key,
                        'Content-Type': 'application/json'}
        self.available = bool(config.supabase_url() and key)

    def save(self, tenant, image_url, path, source, cache_key=None):
        evidence = reviewed_asset(path)
        if not self.available or not tenant or not image_url or not evidence:
            raise ValueError('Reviewed artifact evidence unavailable')
        if not source.get('source_id') or not source.get('source_hash'):
            raise ValueError('Brain source identity unavailable')
        # Retain audit fields without local paths or the verbose provider prompt.
        record = {k: v for k, v in evidence.items() if k not in ('path', 'prompt')}
        response = self.http.post(self.url,
            params={'on_conflict': 'tenant,image_url'},
            headers={**self.headers, 'Prefer': 'resolution=merge-duplicates'},
            json={'tenant': tenant, 'image_url': image_url,
                  'image_sha256': evidence['image_sha256'], 'evidence': record,
                  'source_identity': source, 'cache_key': cache_key}, timeout=30)
        if response.status_code >= 300:
            raise RuntimeError('Artifact evidence persistence failed')
        return record

    def cached(self, tenant, cache_key):
        if not self.available:
            return None
        response = self.http.get(self.url, headers=self.headers, params={
            'tenant': 'eq.' + tenant, 'cache_key': 'eq.' + cache_key,
            'select': 'image_url,evidence', 'order': 'created_at.desc', 'limit': '1'}, timeout=10)
        if response.status_code >= 300:
            raise RuntimeError('Artifact evidence lookup failed')
        rows = response.json()
        if (rows and rows[0].get('evidence', {}).get('policy_version') == POLICY_VERSION
                and rows[0]['evidence'].get('brain_snapshot') == brain_snapshot()):
            return rows[0]['image_url']
        return None

    def claim(self, tenant, cache_key, owner):
        if not self.available:
            return False
        response = self.http.post(self.url.rsplit('/', 1)[0] + '/rpc/claim_infographic_job',
            headers=self.headers, json={'p_tenant': tenant, 'p_key': cache_key,
                'p_owner': owner}, timeout=10)
        if response.status_code >= 300:
            raise RuntimeError('Artifact job claim failed')
        return response.json() is True

    def release(self, tenant, cache_key, owner):
        # A short shared cooldown also protects retries after failed rendering.
        from datetime import datetime, timedelta, timezone
        response = self.http.patch(self.url.rsplit('/', 1)[0] + '/echo_infographic_jobs',
            headers=self.headers, params={'tenant': 'eq.' + tenant,
                'cache_key': 'eq.' + cache_key, 'owner': 'eq.' + owner},
            json={'lease_until': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()},
            timeout=10)
        if response.status_code >= 300:
            raise RuntimeError('Artifact job release failed')
