"""
Slack/SQLite drafts-lane double-publish incident.

Read-only audit finding: the Slack listener's _act() reads a draft via a plain
PendingStore.get() -- no conditional update -- then calls
approvals.handle_action("approve", ...), which only flipped draft.status to
APPROVED *after* publisher.publish() returned. Two concurrent approvals of the
SAME draft_id (a Slack retry on a slow ack, a double tap by the approver, or
two listener replicas) could both read the row while it was still PENDING and
both reach publish(): the only guard was meta_publisher's 24h content-hash
dedup, which is check-then-act (reads before the network call, stamps only
after a successful publish), so two callers that both checked before either
stamped both published live.

The fix: approvals.handle_action's approve branch now claims the draft row
atomically (store.claim_for_publish, a compare-and-swap on the SQLite `drafts`
table -- mirrors the conditional-update shape portal_calendar_store.mark_publishing
already uses for the content_calendar lane) BEFORE calling publish(). Only the
claim's winner publishes; the loser is skipped, not errored. A publish failure
releases the claim (store.release_claim) so the draft is retryable, never
stranded.

These tests are fully offline (a fake publisher; a real PendingStore against a
per-test tmp sqlite file from the autouse _isolated_db fixture in conftest.py).
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import approvals, config, meta_publisher  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402
from agent.store import PendingStore  # noqa: E402


def _draft(draft_id="race1", status=DraftStatus.PENDING):
    return Draft(draft_id=draft_id, account_key="lasso_ig", platform="instagram",
                 caption="Leads go cold in minutes.", hashtags=["#LASSOFramework"],
                 creative_path="card.png",
                 creative_public_url="https://cdn.echo.test/echo/lasso_ig/card.png",
                 scheduled_for="2026-07-13T18:30:00+00:00", status=status,
                 day_key="2026-07-13", draft_type="feed")


def _acct():
    return Account(key="lasso_ig", display_name="lasso_ig",
                   platform=Platform.INSTAGRAM,
                   token_env="CLAIM_TOKEN", target_id_env="CLAIM_TARGET")


class BlockingPublisher:
    """Records every call and blocks inside publish() until released, so two
    threads can be made to race INSIDE the approve branch deterministically."""

    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()
        self.gate = threading.Event()   # set to let a blocked publish() proceed
        self.entered = threading.Event()  # set once a publish() call is inside

    def publish(self, draft, account):
        with self._lock:
            self.calls += 1
        self.entered.set()
        self.gate.wait(timeout=5)
        return meta_publisher.PublishResult(ok=True, mode="published", media_id="M1")


class FailingPublisher:
    def __init__(self, exc):
        self.calls = 0
        self.exc = exc

    def publish(self, draft, account):
        self.calls += 1
        raise self.exc


class OkPublisher:
    def __init__(self):
        self.calls = 0

    def publish(self, draft, account):
        self.calls += 1
        return meta_publisher.PublishResult(ok=True, mode="published", media_id="M1")


class SpyLogger:
    def __init__(self):
        self.records = []

    def log_post(self, **kw):
        self.records.append(kw)
        return kw


# ---- (a) two concurrent approves of the SAME draft publish EXACTLY ONCE -------
def test_concurrent_approves_publish_exactly_once(tmp_path):
    store = PendingStore(path=str(tmp_path / "race.db"))
    d = _draft()
    store.put(d)  # persisted PENDING, as it would be before a real Slack card

    pub = BlockingPublisher()
    results = []

    def _approve():
        # each thread reads its OWN Draft object, exactly like listener.py's
        # _act() doing store.get(draft_id) before calling handle_action
        draft = store.get(d.draft_id)
        res = approvals.handle_action(
            "approve", draft, actor_slack_id=config.APPROVER_SLACK_ID,
            publisher=pub, logger=SpyLogger(), account=_acct(), store=store)
        results.append(res)

    t1 = threading.Thread(target=_approve)
    t2 = threading.Thread(target=_approve)
    t1.start()
    # let the first thread get all the way into publish() before starting the
    # second, so both are guaranteed to race on the SAME claimed row rather
    # than happening to interleave by luck
    pub.entered.wait(timeout=5)
    t2.start()
    t2.join(timeout=5)
    pub.gate.set()  # release the first thread's blocked publish()
    t1.join(timeout=5)

    assert pub.calls == 1, "publish() must be called exactly once for one draft"
    oks = [r for r in results if r.ok]
    assert len(oks) == 2, "both calls should report ok (winner published, loser skipped)"
    # exactly one of the two results is the real publish, the other the skip
    details = sorted(r.detail for r in results)
    assert any("Already claimed" in d for d in details)
    assert any("published" in d for d in details)


# ---- (b) a single approve still publishes exactly once ------------------------
def test_single_approve_still_publishes_exactly_once(tmp_path):
    store = PendingStore(path=str(tmp_path / "solo.db"))
    d = _draft(draft_id="solo1")
    store.put(d)
    pub = OkPublisher()
    draft = store.get(d.draft_id)
    res = approvals.handle_action(
        "approve", draft, actor_slack_id=config.APPROVER_SLACK_ID,
        publisher=pub, logger=SpyLogger(), account=_acct(), store=store)
    assert res.ok is True
    assert pub.calls == 1
    assert draft.status == DraftStatus.APPROVED


def test_single_approve_with_no_persisted_row_still_publishes():
    """A draft never persisted to the store (e.g. runner.py's per-account
    autonomy lane approves before its first store.put(), or a Draft built
    directly as in many existing tests) has nothing to race against: the
    claim is granted by default and publish still happens exactly once."""
    d = _draft(draft_id="never_persisted")
    pub = OkPublisher()
    res = approvals.handle_action(
        "approve", d, actor_slack_id=config.APPROVER_SLACK_ID,
        publisher=pub, logger=SpyLogger(), account=_acct())
    assert res.ok is True
    assert pub.calls == 1


# ---- (c) a publish failure leaves the draft retryable and not stuck -----------
def test_media_not_ready_failure_releases_claim_for_retry(tmp_path):
    store = PendingStore(path=str(tmp_path / "retry.db"))
    d = _draft(draft_id="retry1")
    store.put(d)

    held_pub = FailingPublisher(meta_publisher.MediaNotReady("container not FINISHED"))
    draft = store.get(d.draft_id)
    res = approvals.handle_action(
        "approve", draft, actor_slack_id=config.APPROVER_SLACK_ID,
        publisher=held_pub, account=_acct(), store=store)
    assert res.ok is False
    assert "Held" in res.detail

    # the row must NOT be stranded in the transient claiming state: it is
    # readable again, still PENDING, and shows up in list_pending()
    reread = store.get("retry1")
    assert reread is not None
    assert reread.status == DraftStatus.PENDING
    assert "retry1" in [dd.draft_id for dd in store.list_pending()]

    # and a retry actually succeeds
    ok_pub = OkPublisher()
    res2 = approvals.handle_action(
        "approve", store.get("retry1"), actor_slack_id=config.APPROVER_SLACK_ID,
        publisher=ok_pub, account=_acct(), store=store)
    assert res2.ok is True
    assert ok_pub.calls == 1


def test_generic_publish_exception_releases_claim_for_retry(tmp_path):
    store = PendingStore(path=str(tmp_path / "retry2.db"))
    d = _draft(draft_id="retry2")
    store.put(d)

    boom_pub = FailingPublisher(RuntimeError("network blew up"))
    draft = store.get(d.draft_id)
    try:
        approvals.handle_action(
            "approve", draft, actor_slack_id=config.APPROVER_SLACK_ID,
            publisher=boom_pub, account=_acct(), store=store)
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "the generic exception must still propagate (unchanged behavior)"

    reread = store.get("retry2")
    assert reread is not None
    assert reread.status == DraftStatus.PENDING
    assert "retry2" in [dd.draft_id for dd in store.list_pending()]


# ---- (d) deny/kill/edit are unaffected -----------------------------------------
def test_deny_unaffected_by_claim_logic(tmp_path):
    store = PendingStore(path=str(tmp_path / "deny.db"))
    d = _draft(draft_id="deny1")
    store.put(d)
    draft = store.get("deny1")
    res = approvals.handle_action("deny", draft, actor_slack_id=config.APPROVER_SLACK_ID,
                                  account=_acct(), store=store)
    assert res.ok is True
    assert draft.status == DraftStatus.BLOCKED


def test_kill_unaffected_by_claim_logic(tmp_path):
    store = PendingStore(path=str(tmp_path / "kill.db"))
    d = _draft(draft_id="kill1")
    store.put(d)
    draft = store.get("kill1")
    res = approvals.handle_action("kill", draft, actor_slack_id=config.APPROVER_SLACK_ID,
                                  account=_acct(), confirmed=True, store=store)
    assert res.ok is True
    assert "banned" in res.detail.lower()


def test_edit_unaffected_by_claim_logic(tmp_path):
    store = PendingStore(path=str(tmp_path / "edit.db"))
    d = _draft(draft_id="edit1")
    store.put(d)
    draft = store.get("edit1")

    def _redraft(draft, note):
        return _draft(draft_id="edit1-v2")

    res = approvals.handle_action("edit", draft, actor_slack_id=config.APPROVER_SLACK_ID,
                                  note="shorter", redraft_fn=_redraft, account=_acct(),
                                  store=store)
    assert res.ok is True
    assert res.redraft is not None
    assert res.redraft.status == DraftStatus.PENDING


class DryRunPublisher:
    """publish() with AGENT_PUBLISH_ENABLED off: returns NORMALLY, posting
    nothing. This is the DEFAULT configuration, and it raises no exception, so
    neither release path in the approve branch fires on its own."""

    def __init__(self):
        self.calls = 0

    def publish(self, draft, account):
        self.calls += 1
        return meta_publisher.PublishResult(ok=True, mode="would_publish", media_id="")


def test_a_dry_run_approve_leaves_the_draft_approvable_again(tmp_path):
    """THE REGRESSION THE CLAIM ALMOST SHIPPED. Publishing defaults OFF, so
    publish() normally returns mode='would_publish' and raises nothing. The claim
    was released only on EXCEPTION, so the draft stayed in the claiming state and
    every later approve was silently skipped as 'already claimed'. That converts a
    rare double post into a permanent never post: strictly worse than the bug the
    claim exists to fix. A dry run must leave the draft exactly as approvable as it
    was."""
    store = PendingStore(path=str(tmp_path / "dry.db"))
    d = _draft(draft_id="dry1")
    store.put(d)

    dry = DryRunPublisher()
    res = approvals.handle_action(
        "approve", store.get(d.draft_id), actor_slack_id=config.APPROVER_SLACK_ID,
        publisher=dry, logger=SpyLogger(), account=_acct(), store=store)
    assert res.ok is True and dry.calls == 1

    # ...and now, once publishing is armed, the SAME draft must still publish.
    real = OkPublisher()
    res2 = approvals.handle_action(
        "approve", store.get(d.draft_id), actor_slack_id=config.APPROVER_SLACK_ID,
        publisher=real, logger=SpyLogger(), account=_acct(), store=store)
    assert res2.ok is True
    assert real.calls == 1, "the dry run stranded the claim; the post can never go out"


def test_a_real_publish_still_blocks_a_second_approve(tmp_path):
    """The release above must NOT re-open the door for the double post: after a
    genuinely PUBLISHED result the claim stands and a second approve is skipped."""
    store = PendingStore(path=str(tmp_path / "real.db"))
    d = _draft(draft_id="real1")
    store.put(d)

    first = OkPublisher()
    approvals.handle_action(
        "approve", store.get(d.draft_id), actor_slack_id=config.APPROVER_SLACK_ID,
        publisher=first, logger=SpyLogger(), account=_acct(), store=store)
    assert first.calls == 1

    second = OkPublisher()
    approvals.handle_action(
        "approve", store.get(d.draft_id), actor_slack_id=config.APPROVER_SLACK_ID,
        publisher=second, logger=SpyLogger(), account=_acct(), store=store)
    assert second.calls == 0, "the same draft published twice"
