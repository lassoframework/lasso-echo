"""Human approval for client-uploaded media.

Fresh uploads are intentionally not usable by the planner: the sync lane writes
the client's consent record and context, but never infers a moderation outcome or
approval. This module is used only from a trusted local operator shell. A portal
token does not authenticate a media reviewer.
"""

import argparse
import fcntl
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

from . import config, dam, db, ops_alerts
from .accounts import get_account
from .approvals import _is_approver


def _base(account_key):
    key = (account_key or "").strip()
    for suffix in ("_ig", "_fb"):
        if key.endswith(suffix):
            return key[:-len(suffix)]
    return key


def _account_for_base(base):
    return get_account(f"{base}_ig") or get_account(base)


def _library_path(base, account):
    if account is not None and getattr(account, "library_prefix", ""):
        return account.library_prefix
    return os.path.join(config.LIBRARY_PATH, base)


def _media_path(base, asset_name, account):
    # Asset names are generated with intake_web._safe_name.  Refuse any other
    # path shape here so an approval cannot reach a neighboring tenant's library.
    if not asset_name or asset_name != os.path.basename(asset_name):
        return None
    if os.path.splitext(asset_name)[1].lower() not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}:
        return None
    library = os.path.realpath(_library_path(base, account))
    path = os.path.join(library, asset_name)
    if os.path.islink(path) or not os.path.realpath(path).startswith(library + os.sep):
        return None
    return path


def _audit_approval(base, asset_name, actor_id, reviewed_at, note, *, transaction=None):
    """Commit approval provenance before changing the eligibility sidecar.

    db.audit intentionally swallows failures, so the approval gate must write
    its own checked transaction and fail closed on any database error.
    """
    reason = ops_alerts.scrub(
        f"local operator review; actor={actor_id}; consent=granted; "
        f"moderation=clean; at={reviewed_at}; note={note or ''}")[:500]
    if transaction is not None:
        return transaction.execute(
            "INSERT INTO audit (day, account_key, kind, subject, reason) VALUES (?,?,?,?,?)",
            (reviewed_at[:10], base, "media_approval", asset_name[:200], reason),
        ).lastrowid
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO audit (day, account_key, kind, subject, reason) VALUES (?,?,?,?,?)",
            (reviewed_at[:10], base, "media_approval", asset_name[:200], reason),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def approve(account_key, asset_name, actor_id, *, moderation="", note=""):
    """Serialize local approval decisions across operator processes."""
    from . import local_inventory_mutation
    if local_inventory_mutation.enabled():
        if not isinstance(account_key, str) or account_key != account_key.strip():
            return 400, {"ok": False, "error": "canonical account key required"}
        # PG begin/complete may not run while any local approval flock is held.
        # The new operation owns its canonical gym flock only around local effects.
        return _approve_locked(account_key, asset_name, actor_id,
                               moderation=moderation, note=note)
    lock_path = db.db_path() + ".media-approval.lock"
    try:
        with open(lock_path, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return _approve_locked(account_key, asset_name, actor_id,
                                   moderation=moderation, note=note)
    except OSError:
        return 503, {"ok": False, "error": "approval lock unavailable"}


def _write_approval_sidecar(path, updates):
    """Replace complete JSON atomically; readers never see a truncated file."""
    side_path = dam.sidecar_path(path)
    data = dam.read_sidecar(path)
    data.update(updates)
    fd, temporary = tempfile.mkstemp(prefix=".media-approval-", dir=os.path.dirname(side_path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, side_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _reviewed_source_identity(path):
    """Bind validity to one stable file identity and exact byte digest."""
    from pathlib import Path
    from .client_media_sync import _valid_media_file
    from .local_inventory_mutation import MutationHold
    source = Path(path)
    if source.is_symlink() or source.resolve() != source:
        raise MutationHold('local_mutation_review_bytes_changed')
    def identity():
        stat = source.stat()
        return {'device': stat.st_dev, 'inode': stat.st_ino, 'length': stat.st_size,
                'mtime_ns': stat.st_mtime_ns, 'ctime_ns': stat.st_ctime_ns,
                'source_digest': 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest()}
    before = identity()
    if not _valid_media_file(path) or identity() != before:
        raise MutationHold('local_mutation_review_bytes_changed')
    return before


def _fenced_approval(base, path, actor_id, reviewed_at, note, updates, source_identity):
    from pathlib import Path
    from . import local_inventory_mutation as mutation
    cfg = mutation.configured(base, os.path.dirname(path))
    cfg.asset_path(Path(path))
    source_digest = source_identity['source_digest']
    side_path = Path(dam.sidecar_path(path))
    def apply(conn):
        # The review is bound to the exact bytes that were validated before begin.
        if _reviewed_source_identity(path) != source_identity:
            raise mutation.MutationHold('local_mutation_review_bytes_changed')
        current = json.loads(side_path.read_text()) if side_path.exists() else {}
        if not isinstance(current, dict):
            raise mutation.MutationHold('local_mutation_sidecar_invalid')
        audit_id = _audit_approval(base, Path(path).name, actor_id, reviewed_at, note,
                                   transaction=conn)
        current.update(updates)
        mutation.atomic_write_json(side_path, current)
        return {'asset': Path(path).name, 'source_digest': source_digest,
                'source_identity': source_identity,
                'sidecar_digest': mutation.digest(current), 'audit_id': audit_id}
    authority = mutation.MutationAuthority.from_environment()
    try:
        mutation.run(cfg, authority, 'upload_approval',
                     {'asset': Path(path).name, 'source_digest': source_digest,
                      'source_identity': source_identity,
                      'updates': updates, 'note_digest': mutation.digest(note)}, apply)
    finally:
        authority.close()


def _approved_asset_is_currently_usable(path, account_key):
    """Use the planner's local predicate before an approval may close a bridge.

    A clean review alone is not enough: the asset can still be excluded by
    style, rotation, vision, corruption, or another publishability rule.
    """
    try:
        from .client_media_sync import usable_local_creative
        from .library import Creative
        extension = os.path.splitext(path)[1].lower()
        media_type = "video" if extension in {".mp4", ".mov"} else "image"
        return usable_local_creative(
            Creative(path=path, media_type=media_type), account_key)
    except Exception:
        return False


def _rearm_after_approval(base, account_key, path, asset_name):
    """Record a one-time rearm only when this approved identity is usable now."""
    from . import media_bridge
    return media_bridge.rearm_for_new_upload(
        base, asset_name, usable=_approved_asset_is_currently_usable(path, account_key))


def _approve_locked(account_key, asset_name, actor_id, *, moderation="", note=""):
    """Record one human clean-review decision for an uploaded asset.

    ``moderation`` is deliberately not defaulted: the reviewer must explicitly
    submit ``clean`` as their moderation outcome.  No upload reaches eligibility
    without an authorized human and that explicit outcome.
    """
    if not config.portal_approvals_enabled():
        return 403, {"ok": False, "error": "portal approvals are disabled"}

    base = _base(account_key)
    account = _account_for_base(base)
    if account is None:
        return 404, {"ok": False, "error": "unknown media"}
    if not _is_approver(actor_id, account=account):
        return 403, {"ok": False, "error": "not authorized to approve this media"}

    path = _media_path(base, asset_name, account)
    if path is None or not os.path.isfile(path):
        # Same response for malformed/missing paths: never confirm another file.
        return 404, {"ok": False, "error": "unknown media"}
    from . import local_inventory_mutation as mutation
    fenced = mutation.enabled()
    source_identity = None
    try:
        from .client_media_sync import _valid_media_file
        if fenced:
            source_identity = _reviewed_source_identity(path)
        elif not _valid_media_file(path):
            return 409, {"ok": False, "error": "media is not valid for approval"}
    except Exception:
        return 409, {"ok": False, "error": "media is not valid for approval"}

    side = dam.read_sidecar(path)
    if str(moderation or "").strip().lower() != "clean":
        return 400, {"ok": False, "error": "explicit clean moderation review is required"}

    if fenced:
        try:
            mutation.assert_settled(mutation.configured(base, os.path.dirname(path)))
        except Exception:
            return 503, {"ok": False, "error": "inventory mutation requires reconciliation"}

    if (side.get("approved") is True and side.get("review") is False
            and str(side.get("moderation") or "").lower() == "clean"):
        if fenced:
            # A settled-journal observation is not a lease: another mutation
            # can begin immediately afterward. This retry must not delete a
            # bridge episode outside a mutation fence. Hold without mutation
            # until the bridge reconciliation writer adopts the protocol.
            return 503, {"ok": False, "error": "approval recorded; fenced bridge reconciliation required"}
        # A previous attempt may have approved the sidecar but failed while
        # closing the media bridge episode. Retry that repair on idempotent calls.
        try:
            _rearm_after_approval(base, getattr(account, "key", "") or account_key,
                                  path, asset_name)
        except Exception:
            return 503, {"ok": False, "error": "approval recorded; bridge rearm pending, retry"}
        return 200, {"ok": True, "asset": asset_name, "already_approved": True}

    reviewed_at = datetime.now(timezone.utc).isoformat()
    updates = {
            "approved": True,
            "review": False,
            "moderation": "clean",
            "approved_by": str(actor_id or "")[:200],
            "approved_at": reviewed_at,
            "moderation_reviewed_by": str(actor_id or "")[:200],
            "moderation_reviewed_at": reviewed_at,
        }
    if fenced:
        try:
            _fenced_approval(base, path, actor_id, reviewed_at, note, updates, source_identity)
        except Exception:
            return 503, {"ok": False, "error": "inventory mutation pending or unavailable"}
        # Sidecar and audit are committed, and the exact mutation receipt is
        # complete. That receipt does not fence a later bridge deletion: another
        # writer may already have begun. Keep the episode until its reconciliation
        # writer participates in the mutation protocol.
        return 503, {"ok": False, "approval_recorded": True,
                     "inventory_mutation_complete": True,
                     "error": "approval recorded; fenced bridge reconciliation required"}
    else:
        try:
            _audit_approval(base, asset_name, actor_id, reviewed_at, note)
        except Exception:
            return 503, {"ok": False, "error": "approval audit unavailable"}
        try:
            _write_approval_sidecar(path, updates)
        except Exception:
            return 503, {"ok": False, "error": "approval sidecar write failed"}

    # This is intentionally after the durable approval write.  A raw upload, a
    # A raw upload or sync pass can never close a media-bridge episode.
    try:
        _rearm_after_approval(base, getattr(account, "key", "") or account_key,
                              path, asset_name)
    except Exception:
        return 503, {"ok": False, "error": "approval recorded; bridge rearm pending, retry"}
    return 200, {"ok": True, "asset": asset_name, "already_approved": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Locally review one client upload")
    parser.add_argument("account", help="gym account key")
    parser.add_argument("asset", help="asset basename")
    parser.add_argument("--actor", required=True, help="configured reviewer ID; verified by operator")
    parser.add_argument("--moderation", required=True, choices=["clean"],
                        help="explicit visual moderation result")
    parser.add_argument("--note", default="", help="short operator review note")
    args = parser.parse_args(argv)
    status, result = approve(args.account, args.asset, args.actor,
                             moderation=args.moderation, note=args.note)
    print(json.dumps(result, sort_keys=True))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
