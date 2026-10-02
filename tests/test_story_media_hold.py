"""
Story needs-media hold regression tests (stale FIXER PR #195 intent, rebuilt on
current main, 2026-10-02).

Scope: when the daily runner passes surface_gap=True, a Story slot whose reviewed
9:16 render or hosting FAILED is retained as a durable needs-media hold — never
dropped, never a cropped feed card, never publish-ready — and that hold survives
Draft storage, mirrors into content_calendar with a media_not_ready_reason, is
exposed by the portal mapping, and is refused by Supabase approval and every
due-row / atomic publish-claim path. Duplicate-alert suppression for
studio-reported failures is preserved.

Fully offline: studio, hosting, alerts and Supabase HTTP are all stubbed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, stories  # noqa: E402
from agent import portal_social, real_calendar_mirror  # noqa: E402
from agent.portal_calendar_store import SupabaseCalendarStore  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402

DAY = "2027-07-07"  # a Wednesday: a posting day under the default cadence

QUALITY_INFO = {
    "reason": "wordmark above y=.10 and CTA below y=.85 on every attempt",
    "stage": "quality",
    "reported": True,
}
RENDER_UNAVAILABLE_INFO = {"reason": "provider 529", "stage": "render_unavailable",
                           "reported": True}


def _acct(key="lasso_ig"):
    return Account(key=key, display_name=key, platform=Platform.INSTAGRAM,
                   token_env="HOLD_TOKEN", target_id_env="HOLD_TARGET")


def _feed_draft(tmp_path, name="nano_hook_line.png", **kw):
    feed_img = tmp_path / name
    feed_img.write_bytes(b"\x89PNG\r\n\x1a\nFEED")
    base = dict(
        draft_id="feed1", account_key="lasso_ig", platform="instagram",
        caption="c", hashtags=[], creative_path=str(feed_img),
        creative_public_url="https://cdn.test/feed.png",
        scheduled_for=f"{DAY}T18:30:00+00:00", status=DraftStatus.PENDING,
        source_fragments=["Hook line.", "Body line one.", "Body line two."],
    )
    base.update(kw)
    return Draft(**base)


class _AlertCapture:
    def __init__(self):
        self.messages = []

    def alert(self, msg, **_):
        self.messages.append(msg)
        return None


class _StudioStub:
    def __init__(self, result=None, mutate_info=None):
        self.result = result
        self.mutate_info = mutate_info

    def __call__(self, headline, facts, **kwargs):
        info = kwargs.get("failure_info")
        if info is not None and self.mutate_info:
            info.update(self.mutate_info)
        return self.result


def _arm(monkeypatch, tmp_path, studio_result=None, mutate_info=None,
         host_result="https://cdn.test/story.png"):
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(stories.creative_studio, "generate",
                        _StudioStub(result=studio_result, mutate_info=mutate_info))
    monkeypatch.setattr(stories.media_host, "host_media",
                        lambda path, tenant, client=None: host_result)
    alerts = _AlertCapture()
    monkeypatch.setattr(stories.ops_alerts, "alert", alerts.alert)
    return alerts


def _assert_hold(d, *, day=DAY):
    assert d is not None
    assert d.status == DraftStatus.BLOCKED
    assert d.needs_media is True
    assert d.force_approval is True
    assert d.is_story is True
    assert d.day_key == day
    assert d.draft_type == "story"
    assert (d.creative_public_url or "") == ""
    assert (d.creative_path or "") == ""
    assert d.blocked_reason.strip()
    assert "media not ready" in d.blocked_reason.lower()
    # stable story identity: same id the successful render would have used
    assert d.draft_id


# ---- 1. render failure + hosting failure retain a hold under surface_gap ------

def test_render_failure_surface_gap_retains_hold(monkeypatch, tmp_path):
    # Unreported/unknown terminal render failure -> hold AND exactly one alert.
    alerts = _arm(monkeypatch, tmp_path, studio_result=None)
    d = stories.build_story_draft(_acct(), DAY, feed_draft=_feed_draft(tmp_path),
                                  surface_gap=True)
    _assert_hold(d)
    assert len(alerts.messages) == 1
    assert "held" in alerts.messages[0] or "blocked" in alerts.messages[0]


def test_quality_failure_surface_gap_holds_without_duplicate_alert(
        monkeypatch, tmp_path, capsys):
    alerts = _arm(monkeypatch, tmp_path, studio_result=None,
                  mutate_info=dict(QUALITY_INFO))
    d = stories.build_story_draft(_acct(), DAY, feed_draft=_feed_draft(tmp_path),
                                  surface_gap=True)
    _assert_hold(d)
    assert "quality gate" in d.blocked_reason
    assert alerts.messages == []  # studio's own alert stands; no duplicate
    assert "no duplicate story alert" in capsys.readouterr().out


def test_render_unavailable_surface_gap_holds_without_duplicate_alert(
        monkeypatch, tmp_path):
    alerts = _arm(monkeypatch, tmp_path, studio_result=None,
                  mutate_info=dict(RENDER_UNAVAILABLE_INFO))
    d = stories.build_story_draft(_acct(), DAY, feed_draft=_feed_draft(tmp_path),
                                  surface_gap=True)
    _assert_hold(d)
    assert alerts.messages == []


def test_hosting_failure_surface_gap_retains_hold(monkeypatch, tmp_path):
    art = tmp_path / "nano_story_hook.png"
    art.write_bytes(b"\x89PNG\r\n\x1a\nSTORY")
    alerts = _arm(monkeypatch, tmp_path,
                  studio_result={"path": str(art), "route": "stub:model"},
                  host_result=None)
    d = stories.build_story_draft(_acct(), DAY, feed_draft=_feed_draft(tmp_path),
                                  surface_gap=True)
    _assert_hold(d)
    assert "hosting" in d.blocked_reason.lower()
    assert "render succeeded" in d.blocked_reason
    assert len(alerts.messages) == 1
    assert "hosting" in alerts.messages[0].lower()


def test_premade_hosting_failure_surface_gap_retains_hold(monkeypatch, tmp_path):
    feed = _feed_draft(tmp_path, name="reviewed.png")
    premade = tmp_path / "reviewed_story.png"
    premade.write_bytes(b"\x89PNG\r\n\x1a\nSTORY")
    alerts = _arm(monkeypatch, tmp_path, host_result=None)
    monkeypatch.setattr(config, "story_premade_enabled", lambda: True)
    monkeypatch.setattr(config, "lasso_infographic_quality_enabled", lambda _key: False)
    d = stories.build_story_draft(_acct(), DAY, feed_draft=feed,
                                  surface_gap=True)
    _assert_hold(d)
    assert "premade 9:16" in d.blocked_reason
    assert len(alerts.messages) == 1


def test_failure_without_surface_gap_still_skips(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, studio_result=None)
    assert stories.build_story_draft(
        _acct(), DAY, feed_draft=_feed_draft(tmp_path)) is None


def test_non_studio_creative_keeps_existing_skip_policy(monkeypatch, tmp_path):
    # A plain library image with no 9:16 sibling is log-only and returns None
    # even under surface_gap: the hold is only for a FAILED genuine 9:16 render.
    alerts = _arm(monkeypatch, tmp_path)
    d = stories.build_story_draft(
        _acct(), DAY,
        feed_draft=_feed_draft(tmp_path, name="library_photo.png",
                               creative_public_url="https://cdn.test/lib.png"),
        surface_gap=True)
    assert d is None
    assert alerts.messages == []


# ---- 2. the hold survives Draft storage ---------------------------------------

def test_hold_survives_store_round_trip(monkeypatch, tmp_path):
    from agent.store import PendingStore
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(stories.creative_studio, "generate",
                        _StudioStub(result=None))
    monkeypatch.setattr(stories.ops_alerts, "alert", lambda *a, **k: None)
    d = stories.build_story_draft(_acct(), DAY, feed_draft=_feed_draft(tmp_path),
                                  surface_gap=True)
    store = PendingStore(str(tmp_path / "pending.db"))
    store.put(d)
    loaded = {x.draft_id: x for x in store.list_for_account("lasso_ig")}[d.draft_id]
    assert loaded.needs_media is True
    assert loaded.force_approval is True  # must survive persistence
    assert loaded.status == DraftStatus.BLOCKED
    assert loaded.blocked_reason == d.blocked_reason
    assert loaded.is_story is True and loaded.draft_type == "story"
    assert loaded.day_key == DAY


# ---- 3. content_calendar mirror row -------------------------------------------

class _DraftStore:
    def __init__(self, drafts):
        self._drafts = drafts

    def list_for_account(self, account_key):
        return [d for d in self._drafts if d.account_key == account_key]


def _hold_draft():
    return Draft(
        draft_id="storyhold1", account_key="lasso_ig", platform="instagram",
        caption="", hashtags=[], creative_path="", creative_public_url="",
        scheduled_for=f"{DAY}T12:00:00+00:00", status=DraftStatus.BLOCKED,
        blocked_reason="Story media not ready: render failed.",
        source_fragments=["Hook."], is_story=True, day_key=DAY,
        draft_type="story", needs_media=True, force_approval=True)


def test_mirror_includes_hold_with_reason_not_publish_ready():
    rows = real_calendar_mirror.collect_real_drafts(
        "lasso_ig", _DraftStore([_hold_draft()]))
    assert len(rows) == 1
    row = rows[0]
    assert row["format"] == "story"
    assert row["post_date"] == DAY
    assert row["image_url"] == ""
    assert row["media_not_ready_reason"] == "Story media not ready: render failed."
    assert row["status"] == "pending"  # visible hold, never a publish-ready flag
    assert "approved" not in row["status"] and "publish" not in row["status"]


def test_mirror_still_excludes_empty_url_without_hold():
    ghost = Draft(
        draft_id="ghost1", account_key="lasso_ig", platform="instagram",
        caption="c", hashtags=[], creative_path="", creative_public_url="",
        scheduled_for=f"{DAY}T12:00:00+00:00", status=DraftStatus.PENDING,
        is_story=False, day_key=DAY)
    assert real_calendar_mirror.collect_real_drafts(
        "lasso_ig", _DraftStore([ghost])) == []


# ---- 4. portal mapping + approval gate ----------------------------------------

def _hold_row():
    return {
        "id": "row-hold-1", "gym_id": "lasso_ig", "account": "instagram",
        "post_date": DAY, "pillar": "", "format": "story", "caption": "",
        "image_url": "", "status": "pending",
        "media_not_ready_reason": "Story media not ready: render failed.",
        "variant_status": "active", "published_at": None, "late_post_id": None,
    }


def test_portal_post_shape_exposes_media_hold_reason():
    post = portal_social._content_calendar_post(_hold_row(), is_lasso=True)
    assert post["media_not_ready_reason"].startswith("Story media not ready")
    assert post["needs_media"] is True
    assert post["format"] == "story"
    assert post["status"] == "pending"


class _SbFake:
    def __init__(self, row):
        self.row = row
        self.status_writes = []

    def get_row(self, account_key, row_id):
        return self.row if row_id == self.row.get("id") else None

    def set_status(self, account_key, row_id, new_status):
        self.status_writes.append((row_id, new_status))
        return dict(self.row, status=new_status)


def _approve(monkeypatch, row):
    monkeypatch.setattr(portal_social, "_action_gates",
                        lambda *a, **k: None)
    sb = _SbFake(row)
    code, body = portal_social._handle_approve_supabase(
        "lasso_ig", row["id"], "U_owner", None, sb)
    return code, body, sb


def test_approve_rejects_media_not_ready_row(monkeypatch):
    code, body, sb = _approve(monkeypatch, _hold_row())
    assert code == 409
    assert body["ok"] is False
    assert "media not ready" in body["error"]
    assert sb.status_writes == []


def test_approve_rejects_blank_media_even_without_reason(monkeypatch):
    row = _hold_row()
    row["media_not_ready_reason"] = None
    code, body, sb = _approve(monkeypatch, row)
    assert code == 409 and body["ok"] is False
    assert sb.status_writes == []


def test_approve_published_row_stays_idempotent(monkeypatch):
    row = _hold_row()
    row["status"] = "published"
    row["published_at"] = "2027-07-07T13:00:00Z"
    row["image_url"] = "https://cdn.test/story.png"
    row["media_not_ready_reason"] = None
    code, body, sb = _approve(monkeypatch, row)
    assert code == 200 and body["idempotent"] is True
    assert sb.status_writes == []


def test_approve_ready_row_unaffected(monkeypatch):
    row = _hold_row()
    row["image_url"] = "https://cdn.test/story.png"
    row["media_not_ready_reason"] = None
    code, body, sb = _approve(monkeypatch, row)
    assert code == 200 and body["ok"] is True
    assert sb.status_writes == [("row-hold-1", "approved")]


def test_approve_rejects_terminal_row_even_with_media(monkeypatch):
    row = dict(_hold_row(), status="killed", image_url="https://cdn.test/story.png",
               media_not_ready_reason=None)
    code, body, sb = _approve(monkeypatch, row)
    assert code == 409 and body["ok"] is False
    assert sb.status_writes == []


def test_approve_atomic_write_refuses_media_removed_after_read(monkeypatch):
    ready = dict(_hold_row(), image_url="https://cdn.test/story.png",
                 media_not_ready_reason=None)

    class _RacingStore(_SbFake):
        def approve_ready(self, account_key, row_id):
            self.status_writes.append((row_id, "atomic-attempt"))
            return None  # DB row gained a hold before the conditional UPDATE

    monkeypatch.setattr(portal_social, "_action_gates", lambda *a, **k: None)
    sb = _RacingStore(ready)
    code, body = portal_social._handle_approve_supabase(
        "lasso_ig", ready["id"], "U_owner", None, sb)
    assert code == 409 and body["ok"] is False
    assert sb.status_writes == [(ready["id"], "atomic-attempt")]


# ---- 5. due-rows + atomic claim exclusion --------------------------------------

class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, get_payload=None, patch_payload=None, post_resp=None):
        self.calls = []
        self._get = _Resp(200, get_payload or [])
        self._patch = _Resp(200, patch_payload or [])
        self._post = post_resp or _Resp(200, None)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("get", params or {}))
        return self._get

    def patch(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("patch", params or {}, json or {}))
        return self._patch

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("post", url, json))
        return self._post


def _store(http):
    return SupabaseCalendarStore(url="https://sb.test", service_key="k", http=http)


def test_due_rows_excludes_holds_and_blank_media():
    clean = dict(_hold_row(), id="row-ok",
                 image_url="https://cdn.test/ok.png", media_not_ready_reason=None)
    blank = dict(_hold_row(), id="row-blank", media_not_ready_reason=None)
    hold = dict(_hold_row(), id="row-hold")
    http = _FakeHTTP(get_payload=[clean, blank, hold])
    rows = _store(http).due_rows("lasso_ig", DAY)
    assert [r["id"] for r in rows] == ["row-ok"]
    _, params = http.calls[0]
    assert params["media_not_ready_reason"] == "is.null"


def test_mark_publishing_refuses_hold_and_blank_media():
    # Hold row: prefetch shows the reason -> no claim PATCH is even attempted.
    http = _FakeHTTP(get_payload=[_hold_row()],
                     patch_payload=[dict(_hold_row(), status="publishing")])
    assert _store(http).mark_publishing("row-hold-1") is False
    assert not [c for c in http.calls if c[0] == "patch"]

    blank = _hold_row()
    blank["media_not_ready_reason"] = None
    http2 = _FakeHTTP(get_payload=[blank],
                      patch_payload=[dict(blank, status="publishing")])
    assert _store(http2).mark_publishing("row-hold-1") is False
    assert not [c for c in http2.calls if c[0] == "patch"]


def test_mark_publishing_ready_row_claims_with_hold_filter():
    ready = _hold_row()
    ready["image_url"] = "https://cdn.test/ok.png"
    ready["media_not_ready_reason"] = None
    http = _FakeHTTP(get_payload=[ready],
                     patch_payload=[dict(ready, status="publishing")])
    assert _store(http).mark_publishing("row-hold-1") is True
    patch = [c for c in http.calls if c[0] == "patch"][0]
    assert patch[1]["media_not_ready_reason"] == "is.null"
    assert patch[1]["image_url"] == "not.is.null"


def test_patch_media_recovers_hold_without_auto_approval():
    held = _hold_row()
    recovered = dict(held, image_url="https://cdn.test/recovered-story.png",
                     media_not_ready_reason=None)
    http = _FakeHTTP(get_payload=[held], patch_payload=[recovered])
    row = _store(http).patch_media("lasso_ig", held["id"],
                                   recovered["image_url"])
    assert row == recovered
    patch = [call for call in http.calls if call[0] == "patch"][0]
    assert patch[1]["gym_id"] == "eq.lasso_ig"
    assert patch[1]["status"] == "in.(pending,coach_review)"
    assert patch[2] == {"image_url": recovered["image_url"],
                        "media_not_ready_reason": None}
    assert row["status"] == "pending"  # media recovery never approves the row


def test_mark_publishing_unreadable_row_fails_closed():
    http = _FakeHTTP(get_payload=[])  # row vanished / unreadable
    assert _store(http).mark_publishing("row-gone") is False
    assert not [c for c in http.calls if c[0] == "patch"]


def test_mark_publishing_get_error_and_ambiguous_rows_fail_closed():
    ready = dict(_hold_row(), image_url="https://cdn.test/ok.png",
                 media_not_ready_reason=None)
    for response in (_Resp(503, [ready]), _Resp(200, [ready, ready])):
        http = _FakeHTTP(patch_payload=[dict(ready, status="publishing")])
        http._get = response
        assert _store(http).mark_publishing("row-hold-1") is False
        assert not [c for c in http.calls if c[0] == "patch"]


# ---- 6. RPC migration guards the atomic slot claim -----------------------------

def test_claim_rpc_migration_rejects_media_holds():
    sql = open(os.path.join(os.path.dirname(__file__), "..", "migrations",
                            "calendar_claim_media_guard_20261002.sql")).read()
    assert "claim_calendar_publish_slot_owned" in sql
    assert "media_not_ready_reason is null" in sql
    assert "nullif(btrim(coalesce(image_url, '')), '') is not null" in sql
    assert "approve_calendar_row_if_media_ready" in sql
    assert "status = 'pending'" in sql


def test_approve_ready_rpc_returns_only_owned_updated_row():
    ready = dict(_hold_row(), image_url="https://cdn.test/story.png",
                 media_not_ready_reason=None, status="approved")
    http = _FakeHTTP(post_resp=_Resp(200, [ready]))
    assert _store(http).approve_ready("lasso_ig", ready["id"]) == ready
    assert http.calls[0][0] == "post"
    assert http.calls[0][1].endswith("/rpc/approve_calendar_row_if_media_ready")
    assert http.calls[0][2] == {"p_row_id": ready["id"], "p_gym_id": "lasso_ig"}

    http2 = _FakeHTTP(post_resp=_Resp(200, []))
    assert _store(http2).approve_ready("lasso_ig", ready["id"]) is None
