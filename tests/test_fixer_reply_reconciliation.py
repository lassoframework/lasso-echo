from datetime import datetime, timedelta, timezone

import pytest

from agent import fixer_ops, zernio as Z
from agent import fixer_reply_reconciliation as R
from agent import inbox_alerts


NOW = datetime(2026, 9, 19, 5, 0, tzinfo=timezone.utc)
GYM = "topfuel"
PROFILE = "profile-topfuel"


def _status(complete=True):
    return {name: {"ok": True, "complete": complete}
            for name in ("comment", "mention", "review")}


def _identity(source="comment", item_id="comment-1"):
    return {"source": source, "provider": "instagram",
            "account_id": "account-1",
            "container_id": "post-1" if source != "review" else item_id,
            "item_id": item_id}


def _item(source="comment", item_id="comment-1", kind="member_comment"):
    return {"kind": kind, "source": source, "text": "Can I try a class?",
            "created_at": NOW.isoformat(),
            "provider_identity": _identity(source, item_id),
            "provider_evidence": {}}


def _snapshot(items=None, complete=True, captured_at=NOW):
    return R.build_snapshot(GYM, PROFILE, items or [_item()], _status(complete),
                            now=captured_at, snapshot_id="a" * 32)


class ReadOnlyProvider:
    def __init__(self, *, comments=None, reviews=None, profile=PROFILE,
                 comment_complete=True, review_complete=True):
        self.profile = profile
        self.comments = comments if comments is not None else []
        self.reviews = reviews if reviews is not None else []
        self.comment_complete = comment_complete
        self.review_complete = review_complete

    def find_profile_id(self, gym_key):
        assert gym_key == GYM
        return self.profile

    def inbox_post_comments_complete(self, post_id, account_id):
        assert (post_id, account_id) == ("post-1", "account-1")
        return {"comments": self.comments,
                "pagination": {"complete": self.comment_complete}}

    def list_inbox_reviews_complete(self, profile_id):
        assert profile_id == PROFILE
        return {"data": self.reviews,
                "pagination": {"complete": self.review_complete}}

    def __getattr__(self, name):
        if any(word in name for word in ("reply", "hide", "delete", "send", "post")):
            raise AssertionError(f"outbound provider method touched: {name}")
        raise AttributeError(name)


def _comment(*, replies=None, hidden=False, item_id="comment-1"):
    return {"id": item_id, "platform": "instagram", "isHidden": hidden,
            "replies": replies or []}


def _review(*, has_reply=False, item_id="review-1"):
    return {"id": item_id, "platform": "instagram", "accountId": "account-1",
            "hasReply": has_reply}


def test_snapshot_requires_unique_composite_provider_identity():
    with pytest.raises(R.ReconciliationError) as exc:
        R.build_snapshot(GYM, PROFILE, [_item(), _item()], _status(), now=NOW)
    assert (exc.value.code, exc.value.status) == ("duplicate_provider_identity", 409)


def test_partial_page_and_missing_identity_are_persisted_but_never_reconcilable():
    partial = _snapshot(complete=False)
    assert partial["complete"] is False
    store = {partial["snapshot_id"]: partial}
    with pytest.raises(R.ReconciliationError) as exc:
        R.reconcile_snapshot(partial["snapshot_id"], GYM,
                             zernio=ReadOnlyProvider(), store=store, now=NOW)
    assert exc.value.code == "snapshot_incomplete"

    missing = _item()
    missing["provider_identity"]["account_id"] = None
    assert R.build_snapshot(GYM, PROFILE, [missing], _status(), now=NOW)["complete"] is False


def test_missing_old_and_cross_tenant_snapshots_are_refused():
    with pytest.raises(R.ReconciliationError) as exc:
        R.load_snapshot("b" * 32, GYM, store={}, now=NOW)
    assert exc.value.code == "snapshot_not_found"

    old = _snapshot(captured_at=NOW - timedelta(hours=49))
    store = {old["snapshot_id"]: old}
    with pytest.raises(R.ReconciliationError) as exc:
        R.load_snapshot(old["snapshot_id"], GYM, store=store, now=NOW)
    assert exc.value.code == "snapshot_expired"
    with pytest.raises(R.ReconciliationError) as exc:
        R.load_snapshot(old["snapshot_id"], "another-gym", store=store,
                        now=NOW - timedelta(hours=48))
    assert exc.value.code == "snapshot_tenant_mismatch"


def test_comment_requires_exact_identity_and_explicit_owner_reply():
    snap = _snapshot()
    store = {snap["snapshot_id"]: snap}
    owner_reply = {"id": "reply-1", "from": {"isOwner": True}}
    result = R.reconcile_snapshot(
        snap["snapshot_id"], GYM,
        zernio=ReadOnlyProvider(comments=[_comment(replies=[owner_reply])]),
        store=store, now=NOW)
    assert result["complete"] is True and result["resolved"] is True
    assert result["items"][0]["provider_evidence"] == {"owner_reply_ids": ["reply-1"]}

    wrong = ReadOnlyProvider(comments=[_comment(item_id="another-comment")])
    result = R.reconcile_snapshot(snap["snapshot_id"], GYM, zernio=wrong,
                                  store=store, now=NOW)
    assert result["complete"] is False and result["resolved"] is False
    assert result["items"][0]["status"] == "needs_human"


def test_spam_hide_and_review_reply_use_explicit_provider_flags_only():
    spam_snap = _snapshot(items=[_item(kind="spam")])
    spam = R.reconcile_snapshot(
        spam_snap["snapshot_id"], GYM,
        zernio=ReadOnlyProvider(comments=[_comment(hidden=True)]),
        store={spam_snap["snapshot_id"]: spam_snap}, now=NOW)
    assert spam["resolved"] is True
    assert spam["items"][0]["provider_evidence"] == {"is_hidden": True}

    review_item = _item(source="review", item_id="review-1")
    review_snap = _snapshot(items=[review_item])
    reviews = R.reconcile_snapshot(
        review_snap["snapshot_id"], GYM,
        zernio=ReadOnlyProvider(reviews=[_review(has_reply=True)]),
        store={review_snap["snapshot_id"]: review_snap}, now=NOW)
    assert reviews["complete"] is True and reviews["resolved"] is True
    assert reviews["items"][0]["provider_evidence"] == {"has_reply": True}


def test_mentions_always_need_human_even_with_a_complete_snapshot():
    mention = _item(source="mention", item_id="mention-1")
    mention["provider_identity"]["container_id"] = "mention-1"
    snap = _snapshot(items=[mention])
    result = R.reconcile_snapshot(snap["snapshot_id"], GYM,
                                  zernio=ReadOnlyProvider(),
                                  store={snap["snapshot_id"]: snap}, now=NOW)
    assert result["complete"] is True and result["resolved"] is False
    assert result["items"][0] == {
        "identity": R.provider_identity(mention), "source": "mention",
        "status": "needs_human", "provider_evidence": None,
        "reason": "mention_reply_state_not_supported"}


def test_current_partial_page_never_proves_resolution():
    snap = _snapshot()
    result = R.reconcile_snapshot(
        snap["snapshot_id"], GYM,
        zernio=ReadOnlyProvider(
            comments=[_comment(replies=[{"id": "r", "from": {"isOwner": True}}])],
            comment_complete=False),
        store={snap["snapshot_id"]: snap}, now=NOW)
    assert result["complete"] is False and result["resolved"] is False


def test_transport_failure_in_complete_reader_never_proves_resolution():
    class BrokenCompleteReader(ReadOnlyProvider):
        def inbox_post_comments_complete(self, post_id, account_id):
            raise RuntimeError("captured provider transport failure")

    snap = _snapshot()
    result = R.reconcile_snapshot(snap["snapshot_id"], GYM,
                                  zernio=BrokenCompleteReader(),
                                  store={snap["snapshot_id"]: snap}, now=NOW)
    assert result["complete"] is False and result["resolved"] is False
    assert result["items"][0]["reason"] == "comment_read_failed"


def _complete_zernio(pages):
    """Captured-provider-shaped pages keyed by page or ``(path, page)``."""
    calls = []
    client = Z.ZernioClient.__new__(Z.ZernioClient)

    def read(path, params=None):
        calls.append((path, dict(params or {})))
        page = int((params or {}).get("page", 0))
        key = (path, page)
        value = pages[key] if key in pages else pages[page]
        if isinstance(value, Exception):
            raise value
        return value

    client._get = read
    return client, calls


def _comment_page(page, limit, total, pages, rows):
    return {"comments": rows,
            "pagination": {"page": page, "limit": limit,
                           "total": total, "pages": pages},
            "meta": {"captured_provider_shape": True}}


def _review_page(page, limit, total, pages, rows):
    return {"data": rows,
            "pagination": {"page": page, "limit": limit,
                           "total": total, "pages": pages},
            "summary": {"captured_provider_shape": True}}


def _inbox_page(page, limit, total, pages, rows):
    return {"data": rows,
            "pagination": {"page": page, "limit": limit,
                           "total": total, "pages": pages},
            "meta": {"captured_provider_shape": True}}


def test_complete_comment_reader_proves_a_single_captured_provider_page():
    row = _comment(item_id="comment-1")
    client, calls = _complete_zernio({1: _comment_page(1, 2, 1, 1, [row])})
    result = client.inbox_post_comments_complete("post-1", "account-1", limit=2)
    assert result["comments"] == [row]
    assert result["pagination"]["complete"] is True
    assert result["pagination"]["pages_read"] == 1
    assert calls == [("/v1/inbox/comments/post-1",
                      {"accountId": "account-1", "page": 1, "limit": 2})]


def test_complete_review_reader_rejects_duplicate_identity_below_provider_total():
    first = _review(item_id="review-1")
    second = _review(item_id="review-2")
    # The provider repeated review-1 on the final page. Even though its full
    # content is identical, two unique identities cannot satisfy total=3.
    client, calls = _complete_zernio({
        1: _review_page(1, 2, 3, 2, [first, second]),
        2: _review_page(2, 2, 3, 2, [first]),
    })
    with pytest.raises(Z.ZernioPaginationError,
                       match="duplicate_or_missing_provider_identity"):
        client.list_inbox_reviews_complete(PROFILE, limit=2)
    assert [params["page"] for _path, params in calls] == [1, 2]


def test_complete_comment_listing_and_mentions_read_multiple_raw_provider_pages():
    posts = [
        {"id": "post-1", "platform": "instagram", "accountId": "account-1"},
        {"id": "post-2", "platform": "instagram", "accountId": "account-1"},
        {"id": "post-3", "platform": "instagram", "accountId": "account-1"},
    ]
    mentions = [
        {"id": "mention-1", "platform": "instagram", "accountId": "account-1"},
        {"id": "mention-2", "platform": "instagram", "accountId": "account-1"},
        {"id": "mention-3", "platform": "instagram", "accountId": "account-1"},
    ]
    client, calls = _complete_zernio({
        ("/v1/inbox/comments", 1): _inbox_page(1, 2, 3, 2, posts[:2]),
        ("/v1/inbox/comments", 2): _inbox_page(2, 2, 3, 2, posts[2:]),
        ("/v1/inbox/mentions", 1): _inbox_page(1, 2, 3, 2, mentions[:2]),
        ("/v1/inbox/mentions", 2): _inbox_page(2, 2, 3, 2, mentions[2:]),
    })
    listed = client.list_inbox_comments_complete(PROFILE, limit=2)
    found_mentions = client.list_inbox_mentions_complete(PROFILE, limit=2)
    assert listed["data"] == posts
    assert found_mentions["data"] == mentions
    assert listed["pagination"]["complete"] is True
    assert found_mentions["pagination"]["complete"] is True
    assert [(path, params["page"]) for path, params in calls] == [
        ("/v1/inbox/comments", 1), ("/v1/inbox/comments", 2),
        ("/v1/inbox/mentions", 1), ("/v1/inbox/mentions", 2),
    ]


@pytest.mark.parametrize("payload", [
    _comment_page(1, 2, 3, 1, [_comment(), _comment(item_id="comment-2")]),
    _comment_page(1, 2, 2, 1, [_comment()]),
])
def test_complete_reader_rejects_inconsistent_totals_and_page_shapes(payload):
    client, _calls = _complete_zernio({1: payload})
    with pytest.raises(Z.ZernioPaginationError):
        client.inbox_post_comments_complete("post-1", "account-1", limit=2)


def test_complete_reader_rejects_transport_failure_and_page_cap():
    transport, _calls = _complete_zernio({1: RuntimeError("provider unavailable")})
    with pytest.raises(RuntimeError, match="provider unavailable"):
        transport.inbox_post_comments_complete("post-1", "account-1", limit=2)

    capped, _calls = _complete_zernio({
        1: _comment_page(1, 2, 4, 2, [_comment(), _comment(item_id="comment-2")]),
    })
    with pytest.raises(Z.ZernioPaginationError, match="pagination_cap_exceeded"):
        capped.inbox_post_comments_complete("post-1", "account-1", limit=2,
                                            max_pages=1)


def test_complete_reader_rejects_nonadvancing_pages_and_conflicting_duplicates():
    first = _comment()
    second = _comment(item_id="comment-2")
    nonadvancing, _calls = _complete_zernio({
        1: _comment_page(1, 2, 3, 2, [first, second]),
        2: _comment_page(1, 2, 3, 2, [first]),
    })
    with pytest.raises(Z.ZernioPaginationError, match="non_advancing"):
        nonadvancing.inbox_post_comments_complete("post-1", "account-1", limit=2)

    conflicting = dict(first, isHidden=True)
    ambiguous, _calls = _complete_zernio({
        1: _comment_page(1, 2, 3, 2, [first, second]),
        2: _comment_page(2, 2, 3, 2, [conflicting]),
    })
    with pytest.raises(Z.ZernioPaginationError,
                       match="ambiguous_duplicate_provider_identity"):
        ambiguous.inbox_post_comments_complete("post-1", "account-1", limit=2)


def test_authenticated_route_is_tenant_bound_read_only(monkeypatch):
    monkeypatch.setenv(fixer_ops.SECRET_ENV, "secret")
    snap = _snapshot()
    store = {snap["snapshot_id"]: snap}
    provider = ReadOnlyProvider(comments=[_comment(
        replies=[{"id": "reply-1", "from": {"isOwner": True}}])])
    path = (f"{fixer_ops.ROUTE_PREFIX}/reply-reconciliation/{snap['snapshot_id']}"
            f"?gym_key={GYM}")

    assert fixer_ops.handle("GET", path, lambda *_: "", deps={})[0] == 401
    status, body = fixer_ops.handle(
        "GET", path, lambda key, default="": "secret" if key == fixer_ops.HEADER else default,
        deps={"reply_snapshot_store": store,
              "reply_reconciliation_zernio": provider}, now=NOW)
    assert status == 200 and body["resolved"] is True

    wrong_path = path.replace(f"gym_key={GYM}", "gym_key=other-gym")
    status, body = fixer_ops.handle(
        "GET", wrong_path,
        lambda key, default="": "secret" if key == fixer_ops.HEADER else default,
        deps={"reply_snapshot_store": store,
              "reply_reconciliation_zernio": provider}, now=NOW)
    assert (status, body["error"]) == (403, "snapshot_tenant_mismatch")


class CompleteInboxProvider:
    def find_profile_id(self, gym):
        return PROFILE

    def list_inbox_comments_complete(self, profile_id, **kwargs):
        return {"data": [{"id": "post-1", "accountId": "account-1",
                          "platform": "instagram", "commentCount": 1,
                          "createdTime": NOW.isoformat(), "permalink": "https://example/post"}],
                "pagination": {"complete": True}}

    def inbox_post_comments_complete(self, post_id, account_id, **kwargs):
        return {"comments": [{"id": "comment-1", "platform": "instagram",
                              "message": "Can I try a class?", "createdTime": NOW.isoformat(),
                              "from": {"isOwner": False}, "replies": [],
                              "isHidden": False}],
                "pagination": {"complete": True}}

    def list_inbox_mentions_complete(self, profile_id, **kwargs):
        return {"data": [], "pagination": {"complete": True}}

    def list_inbox_reviews_complete(self, profile_id, **kwargs):
        return {"data": [], "pagination": {"complete": True}}


def test_new_delivered_alert_carries_a_durable_snapshot_id(monkeypatch):
    monkeypatch.setenv("AGENT_INBOX_ALERTS", "true")
    kv = {}
    snapshots = {}
    sent = []
    result = inbox_alerts.run(
        gyms=[GYM], zernio=CompleteInboxProvider(), now=NOW,
        notifier=lambda gym, card: sent.append(card) or True,
        kv_get=lambda key, default="": kv.get(key, default),
        kv_set=lambda key, value: kv.__setitem__(key, value),
        snapshot_store=snapshots)
    summary = result["gyms"][0]
    snapshot_id = summary["reply_snapshot_id"]
    assert summary["reply_snapshot_complete"] is True
    assert snapshots[snapshot_id]["complete"] is True
    assert f"Evidence snapshot: {snapshot_id}" in sent[0]
    assert snapshots[snapshot_id]["items"][0]["identity"] == _identity()


def test_raw_complete_capture_is_immutable_and_reconciles_after_owner_reply(monkeypatch):
    """A live-shaped multi-page read, not adapter-provided ``complete`` metadata."""
    monkeypatch.setenv("AGENT_INBOX_ALERTS", "true")
    quiet_posts = [{"id": f"quiet-{i}", "accountId": "account-1",
                    "platform": "instagram", "commentCount": 0,
                    "createdTime": NOW.isoformat(), "permalink": "https://example/quiet"}
                   for i in range(50)]
    active_post = {"id": "post-1", "accountId": "account-1",
                   "platform": "instagram", "commentCount": 1,
                   "createdTime": NOW.isoformat(), "permalink": "https://example/post"}
    pending_comment = {"id": "comment-1", "platform": "instagram",
                       "message": "Can I try a class?", "createdTime": NOW.isoformat(),
                       "from": {"isOwner": False}, "replies": [], "isHidden": False}
    pages = {
        ("/v1/inbox/comments", 1): _inbox_page(1, 50, 51, 2, quiet_posts),
        ("/v1/inbox/comments", 2): _inbox_page(2, 50, 51, 2, [active_post]),
        ("/v1/inbox/comments/post-1", 1): _comment_page(
            1, 25, 1, 1, [pending_comment]),
        ("/v1/inbox/mentions", 1): _inbox_page(1, 25, 0, 0, []),
        ("/v1/inbox/reviews", 1): _review_page(1, 25, 0, 0, []),
    }
    provider, calls = _complete_zernio(pages)
    provider.find_profile_id = lambda gym: PROFILE if gym == GYM else None
    snapshots, kv = {}, {}
    result = inbox_alerts.run(
        gyms=[GYM], zernio=provider, now=NOW, notifier=lambda *_: True,
        kv_get=lambda key, default="": kv.get(key, default),
        kv_set=lambda key, value: kv.__setitem__(key, value),
        snapshot_store=snapshots)
    summary = result["gyms"][0]
    snapshot_id = summary["reply_snapshot_id"]
    original = snapshots[snapshot_id]
    assert summary["reply_snapshot_complete"] is True
    assert original["complete"] is True
    assert ("/v1/inbox/comments", {"profileId": PROFILE, "page": 2, "limit": 50}) in calls
    assert original["items"][0]["provider_evidence"] == {
        "is_hidden": False, "owner_reply_ids": []}

    pages[("/v1/inbox/comments/post-1", 1)] = _comment_page(
        1, 25, 1, 1, [dict(pending_comment, replies=[
            {"id": "owner-reply", "from": {"isOwner": True}}])])
    reconciliation = R.reconcile_snapshot(snapshot_id, GYM, zernio=provider,
                                           store=snapshots, now=NOW)
    assert reconciliation["complete"] is True
    assert reconciliation["resolved"] is True
    # The saved capture remains its original, immutable provider evidence.
    assert snapshots[snapshot_id] == original
