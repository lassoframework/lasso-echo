"""Pinned original-listener event request replay, then existing use settlement.

Never finalizes batches and never chooses fresh media or makes a new UUID.
The existing recovery flag defaults OFF and pins the original SQLite owner.
"""

def run(*, store=None, logger=None, tenant_id=None, settle=True):
    from .. import remote_drive_use, gbp_drive_use_journal as journal
    from . import gbp_drive_use_recovery as listener
    if not remote_drive_use.enabled() or listener.enabled() is False:
        return dict(ok=True, reason='disabled', replayed=0)
    log = logger or (lambda message: print(f'[event-drive-recovery] {message}'))
    try:
        from ..event_drive_use import required_gates
        required_gates()
        if store is None:
            from ..portal_calendar_store import SupabaseCalendarStore
            store = SupabaseCalendarStore()
        authority = getattr(store, '_s', store)
        cursor = journal.event_replay_snapshot(tenant_id)
        operations = journal.event_operations(tenant_id, after=cursor['after'])
    except Exception:
        return dict(ok=False, reason='event original journal recovery hold', replayed=0)
    replayed, held = 0, 0
    for operation in operations:
        try:
            # Admission advances even when this frozen replay stays held. A
            # persistent earliest failure cannot starve later operations.
            cursor = journal.advance_event_replay_cursor(operation, tenant_id, expected_cursor=cursor)
        except journal.JournalHold as exc:
            return dict(ok=False, reason=('event replay slice superseded' if str(exc) == 'event_replay_cursor_stale'
                                        else 'event replay cursor unavailable'),
                        replayed=replayed, replay_held=held)
        except Exception:
            return dict(ok=False, reason='event replay cursor unavailable',
                        replayed=replayed, replay_held=held)
        try:
            required_gates()
            authority.replay_frozen_event_stage(operation['batch_id'], operation['gym_id'])
            replayed += 1
        except Exception:
            held += 1
            log('event frozen stage replay held; original bytes and claim retained')
    # Terminal proof and exact fresh ACTIVE member checks remain exclusively
    # in the original listener. A staged candidate can never consume a use.
    result = (listener.run(store=store, logger=logger, tenant_id=tenant_id) if settle
              else dict(ok=True))
    if settle and tenant_id is not None and any(e['gym_id'] == tenant_id for e in journal.unsettled()):
        result = dict(result, ok=False, reason='current tenant event Drive use unresolved')
    return dict(result, ok=result.get('ok', False) and not held,
                replayed=replayed, replay_held=held)
