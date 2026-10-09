"""Primary feed adapter to the listener's shared, source-bound Drive journal.

The shared claim account retains its historical _gbp name across all writers.
Only exact primary feed members consume a use; same-day mirrors share that use.
Finalization and same-UUID settlement belong to the existing pinned listener job.
"""
from __future__ import annotations

import json


def _required_gates():
    from .portal_calendar_store import forward_reservation_flag, gbp_staged_journal_flag
    from .jobs import gbp_drive_use_recovery as recovery
    if (forward_reservation_flag() is not True or gbp_staged_journal_flag() is not True
            or recovery.enabled() is not True):
        raise ValueError("feed remote use requires staged journal and listener recovery")
    # This verifies the original pinned database without creating a new owner.
    recovery._durable_path()


def freeze_pick(gym_id, asset, claim_id, post_date, store):
    """Return immutable validated snapshots without consuming or completing use."""
    from . import gbp_planner, remote_drive_use
    _required_gates()
    pick = dict(base=gym_id, asset=asset, store=store)
    snapshots = gbp_planner._authoritative_drive_snapshots(pick)
    if snapshots is None or not claim_id:
        raise ValueError("feed authoritative source snapshots unavailable")
    asset_row, source_row = snapshots
    epoch_id = gbp_planner._drive_use_epoch_id(gym_id)
    # Validate complete CAS contract now, before emitting a draft.
    import uuid
    remote_drive_use.request_for(asset_row, source_row, gym_id=gym_id,
        epoch_id=epoch_id, post_date=post_date, use_id=str(uuid.uuid4()))
    return json.loads(json.dumps(dict(gym_id=gym_id, asset_before=asset_row,
        source_before=source_row, claim_id=claim_id, epoch_id=epoch_id,
        post_date=post_date), sort_keys=True))


def stage_callback(gym_id, drafts):
    """Freeze one use per source against the exact canonical staged feed member."""
    from . import remote_drive_use
    pending = [getattr(d, "_drive_use_pending", None) for d in drafts
               if not getattr(d, "is_story", False)]
    pending = [p for p in pending if p is not None]
    if not pending:
        return None
    if not remote_drive_use.enabled():
        raise ValueError("feed use flag changed during planning")
    _required_gates()
    frozen = json.loads(json.dumps(pending, sort_keys=True))

    def before_forward_stage(request):
        from . import gbp_drive_use_journal as journal
        _required_gates()
        if not remote_drive_use.enabled():
            raise ValueError("feed stage authority changed or tenant mismatch")
        seen, prepared = set(), []
        rows = [member["row"] for member in request["members"]]
        for pick in frozen:
            asset = pick["asset_before"]
            asset_id = asset["id"]
            if pick["gym_id"] != gym_id or asset_id in seen:
                raise ValueError("feed source ownership duplicate or mismatch")
            seen.add(asset_id)
            matches = [r for r in rows if r.get("source_media_asset_id") == asset_id
                       and r.get("account") == "instagram" and r.get("format") != "story"
                       and r.get("post_date") == pick["post_date"]]
            if len(matches) != 1:
                raise ValueError("exact primary feed member unavailable")
            row = matches[0]
            bound = journal.forward_stage_for_member(str(row.get("id") or ""))
            if (row.get("gym_id") != gym_id or bound is None
                    or json.loads(bound["request_text"]) != request
                    or not journal.forward_stage_tenant_matches(dict(gym_id=gym_id), bound)):
                raise ValueError("feed verified raw/canonical stage binding unavailable")
            prepared.append(journal.prepare(dict(
                gym_id=gym_id, logical_post_id=row["logical_post_id"],
                claim_id=pick["claim_id"], post_date=pick["post_date"],
                content_hash=asset["content_hash"], asset_id=asset_id,
                source_id=pick["source_before"]["id"], calendar_row=row,
                payload=row, asset_before=asset, source_before=pick["source_before"],
                epoch_id=pick["epoch_id"])))
        for entry in prepared:
            if entry["state"] == "prepared":
                journal.record_write_intent(entry["use_id"])
            elif entry["state"] not in ("write_intent", "unknown_result"):
                raise ValueError("feed use already landed; refusing another stage")
    return before_forward_stage


def recover(*, store=None, logger=None, tenant_id=None):
    """Run the original pinned listener recovery before planning early exits."""
    from . import remote_drive_use
    if not remote_drive_use.enabled():
        return None
    try:
        _required_gates()
        from .jobs.gbp_drive_use_recovery import run
        result = run(store=store, logger=logger, tenant_id=tenant_id)
        if result.get("ok") and tenant_id is not None:
            from . import gbp_drive_use_journal as journal
            # A bounded recovery wave may leave own entries outside its slice;
            # prepared or unknown entries must still fence this tenant's build.
            if any(entry["gym_id"] == tenant_id for entry in journal.unsettled()):
                result = dict(result, ok=False, reason="current tenant Drive uses unresolved")
        if not result.get("ok"):
            result.setdefault("reason", "current tenant Drive use recovery hold")
        return result
    except Exception:
        return dict(ok=False, reason="feed original Drive journal recovery hold")
