"""
A calendar rebuild (the nightly delete-then-insert every plan/mirror/client lane runs)
must NEVER destroy or duplicate a HUMAN OWNED row. This is the fix for Dale's approvals
reverting to "waiting on you": the rebuild used to delete the whole month (his approved
posts included) and re-insert fresh 'pending' rows.

Covers:
  * delete_month(preserve_human=True) only deletes wipeable rows (adds the status guard).
  * delete_month(preserve_human=False) does a full wipe (no status guard).
  * locked_slots() returns only the human-owned cells.
  * preserve_and_prune() drops rows that collide with a locked slot, keeps the rest, and
    is safe when the store has no locked_slots / when the read fails.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_calendar_store as pcs  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, get_resp=None, delete_resp=None):
        self.calls = []
        self._get_resp = get_resp or _Resp(200, [])
        self._delete_resp = delete_resp or _Resp(200, [])

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("get", url, params or {}, headers or {}))
        return self._get_resp

    def delete(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("delete", url, params or {}, headers or {}, json))
        return self._delete_resp


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    yield


def _row(gym_id="eng", post_date="2026-08-13", account="instagram",
         fmt="feed", status="pending"):
    return {"gym_id": gym_id, "post_date": post_date, "account": account,
            "format": fmt, "status": status}


# ---- the invariant pin: what counts as WIPEABLE ---------------------------

def test_human_owned_statuses_are_never_wipeable():
    """GUARD: if any of these ever creep into _WIPEABLE_STATUSES, a rebuild would
    silently delete a client's approved/published post again (Dale's original bug).
    This test is the tripwire on that constant."""
    human_owned = ("approved", "published", "publishing",
                   "denied", "killed", "failed")
    for s in human_owned:
        assert s not in pcs._WIPEABLE_STATUSES, (
            f"{s!r} must stay OUT of _WIPEABLE_STATUSES or approvals revert")


def test_wipeable_set_is_exactly_the_machine_draft_statuses():
    assert set(pcs._WIPEABLE_STATUSES) == {"pending", "draft", "queued"}


# ---- delete_month status guard -------------------------------------------

def test_delete_month_preserves_human_rows_by_default(monkeypatch):
    http = _FakeHTTP(delete_resp=_Resp(200, []))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    pcs.SupabaseCalendarStore().delete_month("eng", "2026-08")
    method, url, params, headers, _ = next(call for call in http.calls
                                            if call[0] == "delete")
    assert method == "delete"
    assert params["gym_id"] == "eq.eng"
    # the status guard: only NULL or a wipeable status is deleted
    assert params["or"] == "(status.is.null,status.in.(pending,draft,queued))"


def test_delete_month_full_wipe_when_preserve_off(monkeypatch):
    http = _FakeHTTP(delete_resp=_Resp(200, []))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    pcs.SupabaseCalendarStore().delete_month("eng", "2026-08", preserve_human=False)
    _, _, params, _, _ = next(call for call in http.calls
                              if call[0] == "delete")
    assert "or" not in params            # no status guard -> deletes everything


def test_delete_month_preserves_media_hold_rows(monkeypatch):
    """A wipeable-status row with a recorded media_not_ready_reason remains
    held across rebuilds. The server-side is.null filter avoids a race with
    another process applying the hold before the DELETE runs."""
    http = _FakeHTTP(delete_resp=_Resp(200, []))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    pcs.SupabaseCalendarStore().delete_month("eng", "2026-08")
    _, _, params, _, _ = next(call for call in http.calls
                              if call[0] == "delete")
    assert params["media_not_ready_reason"] == "is.null"
    # gym/date scoping and the status guard are untouched alongside the hold guard
    assert params["gym_id"] == "eq.eng"
    assert params["or"] == "(status.is.null,status.in.(pending,draft,queued))"


def test_delete_month_full_wipe_also_deletes_media_holds(monkeypatch):
    """preserve_human=False is a deliberate full wipe: NO status guard and NO
    media-hold guard, so rows held on media_not_ready_reason are deleted too."""
    http = _FakeHTTP(delete_resp=_Resp(200, []))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    pcs.SupabaseCalendarStore().delete_month("eng", "2026-08", preserve_human=False)
    _, _, params, _, _ = next(call for call in http.calls
                              if call[0] == "delete")
    assert "or" not in params
    assert "media_not_ready_reason" not in params


# ---- locked_slots --------------------------------------------------------

def test_locked_slots_returns_only_human_owned(monkeypatch):
    rows = [
        _row(status="pending"),                                   # wipeable
        _row(account="facebook", status="approved"),              # LOCKED
        _row(fmt="story", status="published"),                    # LOCKED
        _row(post_date="2026-08-14", status="draft"),             # wipeable
        _row(post_date="2026-08-15", status="denied"),            # LOCKED
    ]
    http = _FakeHTTP(get_resp=_Resp(200, rows))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    locked = pcs.SupabaseCalendarStore().locked_slots("eng", "2026-08")
    assert locked == {
        ("2026-08-13", "facebook", "feed"),
        ("2026-08-13", "instagram", "story"),
        ("2026-08-15", "instagram", "feed"),
    }


# ---- preserve_and_prune --------------------------------------------------

class _StoreWithLocks:
    def __init__(self, locked):
        self._locked = locked

    def locked_slots(self, account_key, month):
        return self._locked


def test_prune_drops_colliding_rows_keeps_others():
    locked = {("2026-08-13", "instagram", "feed")}
    incoming = [
        _row(account="instagram", fmt="feed"),        # collides -> dropped
        _row(account="facebook", fmt="feed"),         # kept
        _row(account="instagram", fmt="story"),       # kept
    ]
    kept, n = pcs.preserve_and_prune(_StoreWithLocks(locked), "eng",
                                     ["2026-08"], incoming)
    assert n == 1
    slots = {pcs._slot_key(r) for r in kept}
    assert ("2026-08-13", "instagram", "feed") not in slots
    assert len(kept) == 2


def test_prune_keeps_all_when_store_has_no_locked_slots():
    class _Bare:
        pass
    incoming = [_row(), _row(account="facebook")]
    kept, n = pcs.preserve_and_prune(_Bare(), "eng", ["2026-08"], incoming)
    assert kept == incoming and n == 0


def test_prune_keeps_all_when_read_fails():
    class _Boom:
        def locked_slots(self, *a, **k):
            raise RuntimeError("supabase down")
    incoming = [_row(), _row(fmt="story")]
    kept, n = pcs.preserve_and_prune(_Boom(), "eng", ["2026-08"], incoming)
    assert kept == incoming and n == 0


# ---- the client rebuild lane honors the guard end to end ------------------

class _ApplyStore:
    """Records delete/insert and reports one already-approved slot as locked."""
    def __init__(self, locked):
        self._locked = locked
        self.deleted = []
        self.inserted = []

    def locked_slots(self, account_key, month):
        return self._locked

    def delete_month(self, account_key, month, *, preserve_human=True,
                     preserve_dates=()):
        # the lane must keep asking for a preserving delete, never a full wipe
        assert preserve_human is True
        self.deleted.append((account_key, month))
        return 0

    def insert_rows(self, account_key, rows):
        self.inserted.extend(rows)
        return rows


def test_client_apply_skips_locked_slot_and_still_deletes():
    from agent.client_month_run import _apply
    from datetime import date
    locked = {("2026-08-13", "instagram", "feed")}
    store = _ApplyStore(locked)
    rows = [
        _row(account="instagram", fmt="feed"),        # collides w/ approved -> skip
        _row(account="facebook", fmt="feed"),         # inserted
        _row(account="instagram", fmt="story"),       # inserted
    ]
    out = _apply("eng", rows, date(2026, 8, 13), 30, store, lambda m: None)
    assert out["ok"] is True
    assert store.deleted                              # delete lane still runs
    inserted_slots = {pcs._slot_key(r) for r in store.inserted}
    assert ("2026-08-13", "instagram", "feed") not in inserted_slots
    assert out["upserted"] == 2


# ---- wave 2: locked-day SIBLINGS survive the rebuild delete -----------------

def test_delete_month_preserve_dates_excludes_locked_days(monkeypatch):
    """A locked day's still-pending siblings (FB mirror + story of an approved feed)
    must survive the rebuild delete: the builder emits no replacement for a locked
    day, so wiping them would orphan the approved post's cross-post forever."""
    http = _FakeHTTP(delete_resp=_Resp(200, []))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    pcs.SupabaseCalendarStore().delete_month(
        "eng", "2026-08", preserve_dates=("2026-08-13", "2026-08-20"))
    _, _, params, _, _ = next(call for call in http.calls
                              if call[0] == "delete")
    assert params["post_date"] == [
        "gte.2026-08-01", "lte.2026-08-31",
        "not.in.(2026-08-13,2026-08-20)",
    ]
    # the human-status guard still applies on top
    assert params["or"] == "(status.is.null,status.in.(pending,draft,queued))"


def test_client_apply_passes_locked_days_to_delete():
    from agent.client_month_run import _apply
    from datetime import date

    class _Rec:
        def __init__(self):
            self.calls = []

        def locked_slots(self, *a):
            return set()

        def delete_month(self, base_key, month, *, preserve_human=True,
                         preserve_dates=()):
            self.calls.append((month, tuple(sorted(preserve_dates))))
            return 0

        def insert_rows(self, base_key, rows):
            return rows

    store = _Rec()
    _apply("eng", [_row()], date(2026, 8, 13), 5, store, lambda m: None,
           locked_days={"2026-08-13"})
    assert store.calls
    preserved = set(store.calls[0][1])
    assert "2026-08-13" in preserved
    assert all(day in preserved for day in (
        "2026-08-01", "2026-08-12", "2026-08-18", "2026-08-31"))
    assert all(day not in preserved for day in (
        "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17"))


class _StateHTTP:
    """Model the actual DELETE predicates and the new counted held-slot read."""
    def __init__(self, rows):
        from copy import deepcopy
        self.rows = deepcopy(rows)
        self.calls = []
        self.hold_read = None

    def get(self, url, params=None, **kw):
        from copy import deepcopy
        params = params or {}
        self.calls.append(('get', params))
        if url.endswith('support_tickets'):
            selected = []
        else:
            selected = [r for r in self.rows if
                        r['gym_id'] == params['gym_id'][3:]
                        and r.get('variant_status') == 'active']
            dates = params.get('post_date', '')
            if isinstance(dates, str) and dates.startswith('in.('):
                wanted = set(dates[4:-1].split(','))
                selected = [r for r in selected if r['post_date'] in wanted]
            elif isinstance(dates, list):
                selected = [r for r in selected if dates[0][4:] <= r['post_date'] <= dates[1][4:]]
            if params.get('media_not_ready_reason') == 'not.is.null':
                if self.hold_read is not None:
                    return self.hold_read
                selected = [r for r in selected if r['media_not_ready_reason'] is not None]
        response = _Resp(200, deepcopy(selected))
        response.headers = {'Content-Range': f'*/{len(selected)}'}
        return response

    def delete(self, url, params=None, **kw):
        params = params or {}
        self.calls.append(('delete', params))
        bounds = params['post_date']
        exclude = set(bounds[2][8:-1].split(',')) if len(bounds) == 3 else set()
        removed = [r for r in self.rows if r['gym_id'] == params['gym_id'][3:]
                   and bounds[0][4:] <= r['post_date'] <= bounds[1][4:]
                   and r['post_date'] not in exclude and r['variant_status'] == 'active'
                   and ('or' not in params or r['status'] is None or r['status'] in pcs._WIPEABLE_STATUSES)
                   and ('media_not_ready_reason' not in params or r['media_not_ready_reason'] is None)]
        self.rows = [r for r in self.rows if r not in removed]
        return _Resp(200, removed)

    def post(self, url, json=None, **kw):
        from copy import deepcopy
        import uuid
        saved = [dict(r, id=str(uuid.uuid4()), variant_status=r.get('variant_status', 'active'),
                      media_not_ready_reason=r.get('media_not_ready_reason'),
                      time_slot=r.get('time_slot'), slot_index=r.get('slot_index')) for r in json]
        self.calls.append(('post', deepcopy(saved)))
        self.rows.extend(deepcopy(saved))
        return _Resp(201, saved)


def _persisted(**overrides):
    row = dict(_row(), id='retained-uuid', variant_status='active', time_slot=None,
               slot_index=None, image_url='https://cdn/rejected.jpg', caption='Old caption',
               media_not_ready_reason='cross_date_media_repeat_needs_new_visual')
    row.update(overrides)
    return row


def _state_store(monkeypatch, rows):
    monkeypatch.setenv('AGENT_PLAN_HORIZON_DAYS', '0')
    monkeypatch.setenv('AGENT_MEDIA_CROSS_DAY_GUARD', 'false')
    monkeypatch.setenv('AGENT_EMPTY_CAPTION_GUARD', 'false')
    monkeypatch.setenv('AGENT_CAPTION_COOLDOWN', 'false')
    monkeypatch.setenv('AGENT_SLOT_DEDUPE', 'false')
    http = _StateHTTP(rows)
    store = pcs.SupabaseCalendarStore(url='https://proj.supabase.co', service_key='offline-test', http=http)
    return store, http


@pytest.mark.parametrize('account,fmt', [('instagram', 'feed'), ('googlebusiness', 'photo')])
@pytest.mark.parametrize('status', ['pending', 'draft', None])
def test_rebuild_retains_hold_and_refuses_changed_content_in_exact_slot(monkeypatch, account, fmt, status):
    held = _persisted(account=account, format=fmt, status=status)
    ready = _persisted(id='ready', post_date='2026-08-14', media_not_ready_reason=None)
    store, http = _state_store(monkeypatch, [held, ready])
    assert store.delete_month('eng', '2026-08') == 1
    assert http.rows == [held]
    proposal = dict(held, id='new-proposal', status='pending', caption='New generated caption',
                    image_url='https://cdn/new.jpg', media_not_ready_reason=None)
    assert store.insert_rows('eng', [proposal]) == []
    assert http.rows == [held]
    assert not any(method == 'post' for method, _ in http.calls)


def test_held_slot_barrier_preserves_numbered_slots_time_slots_and_channel_siblings(monkeypatch):
    held = _persisted(slot_index=0, time_slot='morning')
    store, http = _state_store(monkeypatch, [held])
    blocked = dict(held, caption='New caption', media_not_ready_reason=None)
    allowed = [dict(blocked, slot_index=1), dict(blocked, slot_index=None),
               dict(blocked, account='facebook'), dict(blocked, time_slot='evening'),
               dict(blocked, format='photo'), dict(blocked, post_date='2026-08-14')]
    result = store.insert_rows('eng', [blocked] + allowed)
    assert len(result) == len(allowed)
    assert {pcs._held_slot_key(r) for r in result} == {pcs._held_slot_key(r) for r in allowed}
    assert http.rows[0] == held


def test_null_slot_fields_are_exact_and_not_inferred_as_morning_or_zero(monkeypatch):
    held = _persisted()
    store, http = _state_store(monkeypatch, [held])
    proposal = dict(held, media_not_ready_reason=None)
    assert len(store.insert_rows('eng', [proposal, dict(proposal, time_slot='morning'),
                                       dict(proposal, slot_index=0)])) == 2
    assert http.rows[0] == held


def test_deliberate_full_wipe_removes_hold_and_then_allows_exact_slot(monkeypatch):
    held = _persisted()
    foreign = _persisted(id='foreign', gym_id='other')
    archived = _persisted(id='archived', variant_status='archived')
    outside = _persisted(id='outside', post_date='2026-09-01')
    store, http = _state_store(monkeypatch, [held, foreign, archived, outside])
    assert store.delete_month('eng', '2026-08', preserve_human=False) == 1
    assert http.rows == [foreign, archived, outside]
    assert len(store.insert_rows('eng', [dict(held, media_not_ready_reason=None)])) == 1


@pytest.mark.parametrize('failure', ['http', 'missing_count', 'partial', 'missing_fields', 'malformed', 'foreign'])
def test_uncertain_held_slot_read_refuses_feed_and_gbp_staging(monkeypatch, failure):
    store, http = _state_store(monkeypatch, [])
    response = _Resp(503 if failure == 'http' else 200, [])
    response.headers = {'Content-Range': '*/0'}
    if failure == 'missing_count':response.headers = {}
    if failure == 'partial':response.headers = {'Content-Range': '0-0/2'}
    if failure == 'missing_fields':response._payload = [{'gym_id': 'eng'}];response.headers = {'Content-Range': '0-0/1'}
    if failure == 'malformed':response._payload = [_persisted() | {'slot_index': []}];response.headers = {'Content-Range': '0-0/1'}
    if failure == 'foreign':response._payload = [_persisted(gym_id='other')];response.headers = {'Content-Range': '0-0/1'}
    http.hold_read = response
    assert store.insert_rows('eng', [dict(_persisted(), media_not_ready_reason=None),
                                    dict(_persisted(account='googlebusiness', format='photo'), media_not_ready_reason=None)]) == []
    assert http.rows == []


def test_barrier_does_not_block_story_recovery_or_candidate_variants(monkeypatch):
    store, _ = _state_store(monkeypatch, [_persisted()])
    proposed = [dict(_persisted(), format='story', media_not_ready_reason=None),
                dict(_persisted(), variant_status='candidate', media_not_ready_reason=None)]
    assert pcs._preserve_held_slots(store, 'eng', proposed) == proposed


@pytest.mark.parametrize('fmt', ['feed', 'update', 'story', None])
def test_held_slot_barrier_applies_to_every_exact_format(monkeypatch, fmt):
    held = _persisted(format=fmt)
    store, _ = _state_store(monkeypatch, [held])
    blocked = dict(held, caption='New generated caption', media_not_ready_reason=None)
    sibling = dict(blocked, slot_index=1)
    assert pcs._preserve_held_slots(store, 'eng', [blocked, sibling]) == [sibling]


@pytest.mark.parametrize('account,fmt', [('instagram', 'feed'), ('googlebusiness', 'update')])
def test_client_apply_data_state_keeps_hold_replaces_ready_and_reports_real_delete_count(monkeypatch, account, fmt):
    from datetime import date
    from agent.client_month_run import _apply
    from agent import cadence
    held = _persisted(account=account, format=fmt, time_slot='evening')
    ready = _persisted(id='ready', post_date='2026-08-14', media_not_ready_reason=None)
    store, http = _state_store(monkeypatch, [held, ready])
    monkeypatch.setattr(cadence, 'resolve_posts_per_day', lambda *a, **kw: 1)
    proposals = [dict(held, media_not_ready_reason=None, caption='New held-slot proposal'),
                 dict(ready, caption='New ready-slot caption')]
    result = _apply('eng', proposals, date(2026, 8, 13), 19, store, lambda m: None)
    assert result['ok'] is True
    assert result['deleted'] == result['deleted_total'] == result['inserted'] == 1
    assert http.rows[0] == held
    assert len(http.rows) == 2 and http.rows[1]['caption'] == 'New ready-slot caption'
    assert http.rows[1]['id'] != ready['id']
