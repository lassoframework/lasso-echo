"""
CrossFit ENG live leak (FB post published 2026-08-23 00:02 ET): a caption went out
ending with "[why] Removed word parents and added people, added pets after kids.
Made the wording more inclusive." — internal edit rationale published to a real gym's
audience, publicly called out by a member ("I think we forgot to erase the chat gpt
prompt?"). This is the worst client-facing leak class, so it is closed at FOUR layers:

  1. SOURCE — drafter._strip_llm_scaffold now strips a TRAILING bracketed meta block
     from the LLM output (the old strip was head-only, which is exactly why this
     leaked), and the portal edit path splits a pasted '[why] ...' rationale off the
     note into the edit's `reason` field (brain rule), never the caption.
  2. STAGE GATE — post_quality.caption_issues (and gbp.caption_issues) reject any
     caption carrying a bracketed meta-label ([why]/[reason]/[edit]/[note]...).
  3. PUBLISH FINAL GATE — calendar_autopublish strips a clean meta SUFFIX and
     publishes the real body (self-heal, no human), or HOLDS + alerts when the whole
     caption is scaffolding. Runs unconditionally, not behind AGENT_CALENDAR_GRADE.
  4. BOOK SWEEP — agent.jobs.caption_meta_sweep cleans waiting rows through the
     STATUS-PRESERVING caption patch (an approved row stays approved) and reports
     published carriers for a manual live-platform edit.

All offline: fake stores, no network, no LLM.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import calendar_autopublish as cap
from agent import drafter
from agent import post_quality
from agent import portal_social as ps
from agent.jobs import caption_meta_sweep
from agent.meta_publisher import PublishResult


LEAKED_META = ("[why] Removed word parents and added people, added pets after "
               "kids. Made the wording more inclusive.")
CLEAN_BODY = ("Your family belongs here. People of every age, kids and pets "
              "included, feel at home the moment they walk in the door.")


# ==========================================================================
# 1. SOURCE — the LLM output parser strips a trailing meta block
# ==========================================================================

def test_strip_llm_scaffold_removes_trailing_why_block():
    out = drafter._strip_llm_scaffold(f"{CLEAN_BODY}\n\n{LEAKED_META}")
    assert out == CLEAN_BODY
    assert "[why]" not in out.lower()


def test_strip_llm_scaffold_all_meta_returns_empty():
    # An output that is ONLY rationale becomes "" so the caller's empty-response
    # fallback produces the caption instead of shipping scaffolding.
    assert drafter._strip_llm_scaffold(LEAKED_META) == ""


def test_strip_llm_scaffold_leaves_clean_caption_alone():
    assert drafter._strip_llm_scaffold(CLEAN_BODY) == CLEAN_BODY


# ==========================================================================
# 2. STAGE GATE — the A+ gate rejects meta-label captions
# ==========================================================================

@pytest.mark.parametrize("label", ["[why]", "[Why]", "[WHY]", "[ why ]",
                                   "[reason]", "[Reasons]", "[edit]",
                                   "[edit note]", "[note]", "[rationale]",
                                   "[explanation]", "[changes]"])
def test_caption_issues_rejects_meta_labels(label):
    cap_text = f"{CLEAN_BODY}\n\n{label} internal reasoning text here."
    issues = post_quality.caption_issues(cap_text)
    assert any("edit-rationale" in i for i in issues), (label, issues)


def test_caption_issues_clean_caption_has_no_meta_issue():
    issues = post_quality.caption_issues(CLEAN_BODY)
    assert not any("edit-rationale" in i for i in issues)


def test_gbp_caption_issues_rejects_meta_labels():
    from agent import gbp
    cap_text = ("Carmel families train together here at our gym and the coaches "
                f"know every name. {LEAKED_META}")
    issues = gbp.caption_issues(cap_text)
    assert any("edit-rationale" in i for i in issues)


def test_split_meta_suffix_contract():
    body, meta = post_quality.split_meta_suffix(f"{CLEAN_BODY}\n{LEAKED_META}")
    assert body == CLEAN_BODY
    assert meta == LEAKED_META
    # all-meta: empty body signals HOLD to the caller
    body, meta = post_quality.split_meta_suffix(LEAKED_META)
    assert body == ""
    assert meta == LEAKED_META
    # clean caption: no meta
    body, meta = post_quality.split_meta_suffix(CLEAN_BODY)
    assert (body, meta) == (CLEAN_BODY, "")


# ==========================================================================
# 1b. SOURCE — the portal edit path moves a pasted rationale into `reason`
# ==========================================================================

class _FakeEditStore:
    def __init__(self, rows):
        self._rows = {r["id"]: dict(r) for r in rows}
        self.caption_patches = []

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None
        return dict(r)

    def patch_caption(self, account_key, row_id, new_caption):
        self.caption_patches.append((row_id, new_caption))
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None
        r["caption"] = new_caption
        r["status"] = "pending"
        return dict(r)


@pytest.fixture
def _portal_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AGENT_TENANT_BRAIN_DIR", str(tmp_path / "brains"))
    yield


def test_portal_edit_with_inline_why_saves_body_and_learns_reason(_portal_env):
    from agent import tenant_brain
    store = _FakeEditStore([{
        "id": "uuid-1", "gym_id": "eng", "post_date": "2026-08-22",
        "account": "facebook", "status": "pending", "caption": "old caption",
        "image_url": "https://cdn/x.jpg", "format": "feed", "pillar": "community",
    }])
    note = f"{CLEAN_BODY}\n\n{LEAKED_META}"
    status, body = ps.handle_edit("eng", "uuid-1", "U1", note=note, sb_store=store)
    assert status == 200
    # the caption written to the store is the BODY only — the rationale never lands
    assert store.caption_patches == [("uuid-1", CLEAN_BODY)]
    assert "[why]" not in body["caption"].lower()
    assert body["caption"] == CLEAN_BODY
    # the rationale (label stripped) is captured as the edit's reason...
    assert body["reason_captured"] is True
    assert "Removed word parents" in body["reason"]
    assert "[why]" not in body["reason"].lower()
    # ...and teaches the brain as this gym's style rule
    assert any("Removed word parents" in r
               for r in tenant_brain.style_rules("eng_ig"))


def test_portal_edit_all_meta_note_is_refused(_portal_env):
    store = _FakeEditStore([{
        "id": "uuid-1", "gym_id": "eng", "post_date": "2026-08-22",
        "account": "facebook", "status": "pending", "caption": "old caption",
        "image_url": "https://cdn/x.jpg", "format": "feed", "pillar": "community",
    }])
    status, body = ps.handle_edit("eng", "uuid-1", "U1", note=LEAKED_META,
                                  sb_store=store)
    # nothing publishable in the note: refused, never saved, never silently emptied
    assert status == 400
    assert store.caption_patches == []


def test_split_note_meta_explicit_reason_wins(_portal_env):
    note, reason = ps._split_note_meta(f"{CLEAN_BODY} {LEAKED_META}",
                                       reason="keep it youth focused")
    assert note == CLEAN_BODY
    assert reason == "keep it youth focused"      # the explicit reason is kept


# ==========================================================================
# 3. PUBLISH FINAL GATE — strip a clean suffix / hold an all-meta caption
# ==========================================================================

RUN_DATE = "2026-08-10"
LATE_NOW = "2026-08-10T23:59:00-04:00"


def _row(row_id, caption, status="pending"):
    return {"id": row_id, "gym_id": "lasso", "post_date": RUN_DATE,
            "account": "instagram", "format": "feed", "status": status,
            "caption": caption, "image_url": "https://cdn/x.jpg",
            "published_at": None, "late_post_id": None}


class _FakeCalStore:
    """Minimal due_rows/claim/update fake plus the status-preserving patch."""

    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.preserve_patches = []     # (gym, row_id, caption)

    def due_rows(self, gym_id, run_date):
        return [dict(r) for r in self.rows.values()
                if r.get("gym_id") == gym_id and r.get("post_date") == run_date
                and r.get("status") not in ("published", "denied", "killed")
                and not r.get("published_at") and r.get("image_url")]

    def patch_caption_preserve_status(self, gym_id, row_id, new_caption):
        self.preserve_patches.append((gym_id, row_id, new_caption))
        r = self.rows.get(row_id)
        if r is None:
            return None
        r["caption"] = new_caption          # status DELIBERATELY untouched
        return dict(r)

    def mark_publishing(self, row_id):
        r = self.rows.get(row_id)
        if not r or r.get("status") not in ("pending", "approved") \
                or r.get("published_at"):
            return False
        r["status"] = "publishing"
        return True

    def mark_published(self, row_id, media_id, published_at):
        r = self.rows.get(row_id)
        if r:
            r["status"] = "published"
            r["published_at"] = published_at
        return r

    def mark_publish_failed(self, row_id, revert_status="pending", **kw):
        r = self.rows.get(row_id)
        if r:
            r["status"] = revert_status
        return r


class _FakePublisher:
    def __init__(self):
        self.calls = []

    def __call__(self, draft, account):
        self.calls.append(draft)
        return PublishResult(ok=True, mode="published", media_id="M")


@pytest.fixture
def _armed(monkeypatch):
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")


def test_publish_lane_strips_clean_meta_suffix_and_publishes_body(_armed):
    store = _FakeCalStore([_row("r1", f"{CLEAN_BODY}\n\n{LEAKED_META}")])
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert summary["published"] == ["r1"]
    # the OUTBOUND caption is the clean body — the rationale never hit the network
    assert len(pub.calls) == 1
    assert pub.calls[0].caption == CLEAN_BODY
    assert "[why]" not in pub.calls[0].caption.lower()
    # and the stored caption was cleaned through the status-preserving patch
    assert store.preserve_patches == [("lasso", "r1", CLEAN_BODY)]


def test_publish_lane_holds_all_meta_caption(_armed):
    store = _FakeCalStore([_row("r2", LEAKED_META)])
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    # never claimed, never published, left waiting for a human rewrite
    assert summary["published"] == []
    assert "r2" in summary["waiting"]
    assert pub.calls == []
    assert store.preserve_patches == []
    assert store.rows["r2"]["status"] == "pending"      # untouched


def test_publish_lane_strip_survives_a_store_without_the_patch_method(_armed):
    """A fake/legacy store lacking patch_caption_preserve_status still publishes the
    CLEAN body — the local row is authoritative for the send; persistence is belt."""
    class _NoPatchStore(_FakeCalStore):
        patch_caption_preserve_status = None
    store = _NoPatchStore([_row("r3", f"{CLEAN_BODY} {LEAKED_META}")])
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert summary["published"] == ["r3"]
    assert pub.calls[0].caption == CLEAN_BODY


def test_gbp_worker_strips_meta_suffix_before_send():
    from agent import gbp_worker

    class _Client:
        def __init__(self):
            self.payloads = []

        def create_post_raw(self, payload, draft=True):
            self.payloads.append(payload)
            return {"id": "gbp-post-1"}

    client = _Client()
    row = {"id": "g1", "gym_id": "eng", "post_date": RUN_DATE,
           "caption": ("Carmel families train together here and the coaches know "
                       f"every name in the room. {LEAKED_META}"),
           "image_url": "https://cdn/x.jpg", "gbp_topic_type": "STANDARD"}
    connection = {"zernio_account_id": "acct-1", "gbp_location_id": "loc-1"}
    out = gbp_worker.publish_gbp_row(row, connection, client=client, draft=True)
    assert out["ok"] is True, out
    sent = str(client.payloads[0])
    assert "[why]" not in sent.lower()
    assert "Removed word parents" not in sent


def test_gbp_worker_all_meta_caption_fails_rails():
    from agent import gbp_worker
    row = {"id": "g2", "gym_id": "eng", "post_date": RUN_DATE,
           "caption": LEAKED_META, "image_url": "https://cdn/x.jpg"}
    out = gbp_worker.publish_gbp_row(row, {"zernio_account_id": "a"},
                                     client=None, draft=True)
    assert out["ok"] is False
    assert "empty caption" in out["reject_reason"]


# ==========================================================================
# 4. BOOK SWEEP — clean waiting rows (status preserved), report published ones
# ==========================================================================

class _FakeSweepStore:
    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.preserve_patches = []

    def rows_in_range(self, gym_id, start_iso, end_iso):
        return [dict(r) for r in self.rows.values()
                if r.get("gym_id") == gym_id
                and start_iso <= str(r.get("post_date")) <= end_iso]

    def patch_caption_preserve_status(self, gym_id, row_id, new_caption):
        self.preserve_patches.append((gym_id, row_id, new_caption))
        r = self.rows.get(row_id)
        if r is None:
            return None
        r["caption"] = new_caption          # status untouched, by contract
        return dict(r)


def test_sweep_cleans_waiting_rows_and_preserves_approved_status():
    dirty = f"{CLEAN_BODY}\n\n{LEAKED_META}"
    store = _FakeSweepStore([
        {"id": "w1", "gym_id": "eng", "post_date": "2026-08-24",
         "account": "facebook", "status": "approved", "caption": dirty},
        {"id": "w2", "gym_id": "eng", "post_date": "2026-08-25",
         "account": "instagram", "status": "pending", "caption": CLEAN_BODY},
        {"id": "p1", "gym_id": "eng", "post_date": "2026-08-22",
         "account": "facebook", "status": "published", "caption": dirty},
        {"id": "h1", "gym_id": "eng", "post_date": "2026-08-26",
         "account": "instagram", "status": "pending", "caption": LEAKED_META},
    ])
    alerts = []
    results = caption_meta_sweep.run(gym_ids=["eng"], store=store,
                                     today_iso="2026-08-23",
                                     alert=alerts.append)
    (r,) = results
    # the approved waiting row is cleaned IN PLACE and stays approved
    assert [d["id"] for d in r["cleaned"]] == ["w1"]
    assert store.preserve_patches == [("eng", "w1", CLEAN_BODY)]
    assert store.rows["w1"]["status"] == "approved"
    assert store.rows["w1"]["caption"] == CLEAN_BODY
    # the clean row is untouched
    assert store.rows["w2"]["caption"] == CLEAN_BODY
    # the all-meta row is HELD (a human rewrites it), never emptied
    assert [d["id"] for d in r["held"]] == ["h1"]
    assert store.rows["h1"]["caption"] == LEAKED_META
    # the published carrier is REPORTED (live edit needed), never written to
    assert [d["id"] for d in r["published"]] == ["p1"]
    assert store.rows["p1"]["caption"] == dirty
    assert alerts and "LIVE post" in alerts[0]
    assert "p1" in alerts[0]


def test_sweep_dry_run_writes_nothing():
    dirty = f"{CLEAN_BODY}\n\n{LEAKED_META}"
    store = _FakeSweepStore([
        {"id": "w1", "gym_id": "eng", "post_date": "2026-08-24",
         "account": "facebook", "status": "approved", "caption": dirty},
    ])
    results = caption_meta_sweep.run(gym_ids=["eng"], store=store, dry_run=True,
                                     today_iso="2026-08-23", alert=lambda m: None)
    assert [d["id"] for d in results[0]["cleaned"]] == ["w1"]
    assert store.preserve_patches == []
    assert store.rows["w1"]["caption"] == dirty
