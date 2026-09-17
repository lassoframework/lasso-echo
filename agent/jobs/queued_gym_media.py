"""Durable source-level indexing requests; never stages or publishes posts."""
from __future__ import annotations

from .. import config, media_source_store
from .sync_gym_media import sync_source


def run_one(*, store=None, drive=None, sync=None, log=print):
    store = store or media_source_store.default_store()
    if not store.available():
        return False
    source = store.claim_sync()
    if not source:
        return False
    token = source["sync_claim_token"]
    error = None
    try:
        if not source.get("active") or source.get("kind") != "gym_drive":
            raise ValueError("source is no longer active")
        if not config.gym_drive_connect_active_for(source.get("gym_id")):
            raise ValueError("Drive connection is disabled for this gym")
        result = (sync or sync_source)(source, store=store, drive=drive, log=log,
                                       sweep_missing=False)
        if not result.get("ok"):
            error = result.get("error") or ("Drive access revoked" if result.get("revoked")
                                            else "indexing did not complete")
    except Exception as exc:  # the claimed row must become retryable after failure
        error = type(exc).__name__
        log(f"[gym-media] queued sync failed for {source['id']}: {error}")
    store.finish_sync(source["id"], token, error is None, error)
    return True


def serve(*, interval=15, stop=None, log=print):
    import time
    while stop is None or not stop.is_set():
        try:
            if run_one(log=log):
                continue
        except Exception as exc:
            log(f"[gym-media] queue poll failed: {type(exc).__name__}")
        if stop is None:
            time.sleep(interval)
        else:
            stop.wait(interval)
