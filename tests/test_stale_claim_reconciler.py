from datetime import datetime, timezone

import pytest

from agent import stale_claim_reconciler as scr
from agent import portal_calendar_store as pcs
from agent.zernio import ZernioClient, ZernioPaginationError


NOW = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
TOKEN = "00000000-0000-4000-8000-000000000001"


def row(row_id="row-1", gym="gym-a", image="https://cdn/a.png", **overrides):
    value = {"id": row_id, "gym_id": gym, "account": "instagram",
            "format": "feed", "post_date": "2026-09-30",
            "scheduled_at": "2026-09-30T16:00:00Z", "caption": "Exact words",
            "image_url": image, "publish_claim_token": TOKEN,
            "publish_reservation_day": "2026-09-30"}
    value.update(overrides)
    return value


def live_post(post_id="provider-1", image="https://cdn/a.png"):
    return {"_id": post_id, "content": "Exact words",
            "scheduledFor": "2026-09-30T16:01:00Z",
            "mediaItems": [{"type": "image", "url": image}],
            "platforms": [{"platform": "instagram", "status": "published",
                           "publishedAt": "2026-09-30T16:02:00Z",
                           "platformPostId": "ig-1"}]}


class KV(dict):
    def set(self, key, value):
        self[key] = value


class Store:
    def __init__(self, rows):
        self.rows = rows
        self.published = []
        self.released = []

    def publishing_rows(self):
        return list(self.rows)

    def gym_zernio_profile_id(self, gym):
        return {"gym-a": "profile-a", "gym-b": "profile-b"}.get(gym)

    def reconcile_stale_published(self, gym, rid, token, post_id, published_at):
        self.published.append((gym, rid, token, post_id, published_at))
        return {"id": rid}

    def release_stale_publish_claim(self, gym, rid, token, reason):
        self.released.append((gym, rid, token, reason))
        return {"id": rid}


class Provider:
    def __init__(self, by_profile=None, error=None):
        self.by_profile = by_profile or {}
        self.error = error
        self.calls = []

    def posts_range_complete(self, profile, start, end, **kwargs):
        self.calls.append((profile, start, end, kwargs))
        if self.error:
            raise self.error
        return list(self.by_profile.get(profile, []))


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_ZERNIO_PUBLISH", "true")


def old_kv(*rows):
    return KV({f"stuck_publishing_{r['id']}": "2026-09-30T17:00:00Z"
               for r in rows})


def test_exact_live_match_stamps_and_exact_absence_releases(armed):
    live, absent = row(), row("row-2", image="https://cdn/b.png")
    store = Store([live, absent])
    provider = Provider({"profile-a": [live_post()]})
    out = scr.reconcile(store=store, provider=provider, kv=old_kv(live, absent),
                        now=NOW, alert=lambda _message: None)
    assert [x[1] for x in store.published] == ["row-1"]
    assert [x[1] for x in store.released] == ["row-2"]
    assert store.published[0][0] == store.released[0][0] == "gym-a"
    assert store.published[0][2] == store.released[0][2] == TOKEN
    assert len(provider.calls) == 1       # one bounded read per tenant, not per row
    assert out["provider_reads"] == 1


def test_posted_is_a_terminal_live_status_like_normal_confirmation(armed):
    r = row()
    post = live_post()
    post["platforms"][0]["status"] = "posted"
    store = Store([r])
    out = scr.reconcile(
        store=store, provider=Provider({"profile-a": [post]}), kv=old_kv(r),
        now=NOW, alert=lambda _message: None)
    assert out["published"][0]["id"] == "row-1"
    assert [x[1] for x in store.published] == ["row-1"]
    assert not store.released


def test_incomplete_provider_read_and_missing_media_never_authorize_release(armed):
    r = row()
    store = Store([r])
    failed = scr.reconcile(
        store=store, provider=Provider(error=ZernioPaginationError("partial")),
        kv=old_kv(r), now=NOW, alert=lambda _message: None)
    assert not store.published and not store.released
    assert failed["held"][0]["reason"] == "provider_read_ZernioPaginationError"

    post = live_post()
    post.pop("mediaItems")
    state, evidence = scr.classify(r, [post])
    assert (state, evidence["reason"]) == ("ambiguous", "provider_media_identity_missing")


def test_not_old_enough_and_missing_tenant_binding_remain_held(armed):
    fresh, unbound = row(), row("row-2", gym="unknown")
    kv = KV({"stuck_publishing_row-1": "2026-09-30T19:30:00Z",
             "stuck_publishing_row-2": "alerted:2026-09-30T19:00:00Z"})
    store = Store([fresh, unbound])
    provider = Provider()
    out = scr.reconcile(store=store, provider=provider, kv=kv, now=NOW,
                        alert=lambda _message: None)
    assert not provider.calls and not store.published and not store.released
    assert out["held"] == [{"id": "row-2", "reason": "no_tenant_profile_binding"}]


def test_missing_scheduled_at_uses_atomic_reservation_day(armed):
    r = row(scheduled_at=None)
    store = Store([r])
    provider = Provider({"profile-a": []})
    out = scr.reconcile(store=store, provider=provider, kv=old_kv(r),
                        now=NOW, alert=lambda _message: None)
    assert [x[1] for x in store.released] == ["row-1"]
    assert out["released"][0]["id"] == "row-1"
    # Midnight minus the safety day, rather than holding forever for optional
    # display metadata that failed to stamp.
    assert provider.calls[0][1].isoformat() == "2026-09-29T00:00:00+00:00"


def test_legacy_row_without_schedule_or_reservation_uses_post_date(armed):
    r = row(scheduled_at=None, publish_reservation_day=None)
    store = Store([r])
    provider = Provider({"profile-a": []})
    scr.reconcile(store=store, provider=provider, kv=old_kv(r),
                  now=NOW, alert=lambda _message: None)
    assert [x[1] for x in store.released] == ["row-1"]


def test_legacy_client_profile_falls_back_to_publishers_local_binding(
        armed, monkeypatch):
    r = row(gym="legacy-gym")
    store = Store([r])
    provider = Provider({"profile-legacy": []})
    monkeypatch.setattr(
        scr, "_local_profile_id",
        lambda gym, platform: "profile-legacy"
        if (gym, platform) == ("legacy-gym", "instagram") else None)
    out = scr.reconcile(store=store, provider=provider, kv=old_kv(r),
                        now=NOW, alert=lambda _message: None)
    assert out["released"][0]["id"] == "row-1"
    assert provider.calls[0][0] == "profile-legacy"


class PagedClient(ZernioClient):
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list_posts(self, profile_id, page=1, limit=50):
        self.calls.append((profile_id, page, limit))
        return self.pages[page - 1]


def test_provider_range_reader_requires_complete_bounded_pagination():
    pages = [
        {"posts": [live_post("new")], "pagination": {"total": 2, "pages": 2}},
        {"posts": [{**live_post("old"), "scheduledFor": "2026-09-28T12:00:00Z"}],
         "pagination": {"total": 2, "pages": 2}},
    ]
    client = PagedClient(pages)
    posts = client.posts_range_complete(
        "profile-a", "2026-09-29T00:00:00Z", NOW, page_limit=1, max_pages=2)
    assert [p["_id"] for p in posts] == ["new"]
    assert len(client.calls) == 2

    truncated = PagedClient([
        {"posts": [live_post("new")], "pagination": {"total": 2, "pages": 2}},
    ])
    with pytest.raises(ZernioPaginationError):
        truncated.posts_range_complete(
            "profile-a", "2026-09-01T00:00:00Z", NOW,
            page_limit=1, max_pages=1)


class Response:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class PatchHTTP:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def patch(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append((url, params, json))
        return Response(self.payload)


def test_store_reconciliation_writes_are_tenant_and_claim_scoped(monkeypatch):
    published = {"id": "row-1", "gym_id": "gym-a", "status": "published",
                 "late_post_id": "provider-1"}
    http = PatchHTTP([published])
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    store = pcs.SupabaseCalendarStore()
    assert store.reconcile_stale_published(
        "gym-a", "row-1", TOKEN, "provider-1", NOW.isoformat()) == published
    params = http.calls[0][1]
    assert params == {"id": "eq.row-1", "gym_id": "eq.gym-a",
                      "status": "eq.publishing", "published_at": "is.null",
                      "late_post_id": "is.null",
                      "publish_claim_token": f"eq.{TOKEN}"}

    released = {"id": "row-1", "gym_id": "gym-a", "status": "approved"}
    http.payload = [released]
    assert store.release_stale_publish_claim(
        "gym-a", "row-1", TOKEN, "provider proved absent") == released
    params, body = http.calls[1][1:]
    assert params["gym_id"] == "eq.gym-a"
    assert params["publish_claim_token"] == f"eq.{TOKEN}"
    assert body["status"] == "approved"
    assert body["publish_reservation_day"] is None
