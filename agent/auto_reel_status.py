"""Shared, tenant-scoped status projection for portal and worker on separate disks."""
from datetime import datetime, timezone
from .story_studio_store import SupabaseStoryStudioStore


def publish(gym, snapshot, *, store=None):
    st=store or SupabaseStoryStudioStore()
    if not st.available():
        return False
    try:
        response=st._client().post(st._rest('auto_reel_status'),
            params={'on_conflict':'gym_id'},
            headers=st._headers({'Content-Type':'application/json','Prefer':'resolution=merge-duplicates'}),
            json={'gym_id':gym,'snapshot':snapshot,'updated_at':datetime.now(timezone.utc).isoformat()},timeout=10)
        return response.status_code < 300
    except Exception:
        return False


def read(gym, *, store=None):
    st=store or SupabaseStoryStudioStore()
    unavailable={'ok':False,'jobs':[],'reason':'Live automatic reel status is unavailable'}
    if not st.available():
        return unavailable
    try:
        response=st._client().get(st._rest('auto_reel_status'),
            params={'gym_id':'eq.'+gym,'select':'gym_id,snapshot,updated_at','limit':'1'},headers=st._headers(),timeout=10)
        response.raise_for_status()
        rows=response.json()
        if not rows or rows[0].get('gym_id') != gym:
            return unavailable
        ts=datetime.fromisoformat(rows[0]['updated_at'].replace('Z','+00:00'))
        age=(datetime.now(timezone.utc)-ts).total_seconds()
        if not 0 <= age <= 900:
            return unavailable
        snapshot=rows[0]['snapshot']
        return snapshot if snapshot.get('gym') == gym else unavailable
    except Exception:
        return unavailable


def publish_many(snapshots, *, store=None):
    """One bounded heartbeat write for the fleet, never a job-state mutation."""
    st=store or SupabaseStoryStudioStore()
    if not st.available() or not snapshots:
        return False
    now=datetime.now(timezone.utc).isoformat()
    try:
        response=st._client().post(st._rest('auto_reel_status'),
            params={'on_conflict':'gym_id'},
            headers=st._headers({'Content-Type':'application/json','Prefer':'resolution=merge-duplicates'}),
            json=[{'gym_id':gym,'snapshot':snapshot,'updated_at':now} for gym,snapshot in snapshots.items()],timeout=10)
        return response.status_code < 300
    except Exception:
        return False
