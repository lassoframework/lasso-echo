"""The Slack noise gate, and the three alert shapes that caused it.

2026-09-03, Blake, looking at ~20 alerts from one morning in #echoclaude: "are these true
errors, if so they need fixed. if not stop having them show up in the slack."

Two were true errors. Eight named real human onboarding work (connect links, a key
mismatch) and correctly stay NEEDS_TRIAGE. The remaining ten were self-describing
informational lines, several of them the content rails REPORTING THEMSELVES WORKING.

This file pins, using the verbatim production text:
  * the informational shapes are NOISE
  * everything that names real work is still NEEDS_TRIAGE
  * the gate only drops NOISE, only when armed, and never drops a forced alert
  * the audit write happens BEFORE the gate, so a suppressed alert is still on the record
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import ops_triage as ot  # noqa: E402


# ---- the real lines from that morning -------------------------------------------------

GBP_NOTHING_PLANNED = (
    "ECHO ALERT: GBP month sweep: crossfitreverb30b5b2 is connected to Google Business "
    "but its month could not be planned (nothing planned (no A+ captions or media)). "
    "Nothing was written and nothing was fabricated.")

EVENT_ARC_ADVISORY = (
    "ECHO ALERT: event arc top-up: theboltonclub/evt_bring-a-friend-week_32b838bfad "
    "month grades 88 (B) after remediation; staging anyway (advisory gate — top-up of an "
    "already-admitted client event; rows land pending).")

REGISTERED_BUILD_LANE = (
    "ECHO ALERT: crossfitnine782f21b: registered into Echo's account registry as "
    "'CrossFit Nine 7' so it is in the build lane. It still needs its own intake, "
    "connection and media before anything real can post.")

NOT_CONNECTED = (
    "ECHO ALERT: crossfitnewtown: not set up to post (not_connected). a Zernio profile "
    "exists but ZERO platforms are connected. Send the gym its connect link "
    "(python -m agent intake-link --account <key>).")

KEY_MISMATCH = (
    "ECHO ALERT: crossfitnine782f21b: not set up to post (key_mismatch, no_sources, "
    "no_profile). its intake forwarded under a DIFFERENT account key, so its answers "
    "landed where nothing reads them. Migrate the sources onto the portal key.")

GBP_PHOTO_FAILED = (
    "ECHO ALERT: GBP photo drop failed for crossfitnine7f7dadc row "
    "333f90a3-429b-46a1-b9f8-a91af1166cc8: ZernioError: zernio 400: Invalid request to "
    "Google Business Profile")


def test_the_informational_shapes_are_noise():
    for line in (GBP_NOTHING_PLANNED, EVENT_ARC_ADVISORY, REGISTERED_BUILD_LANE):
        assert ot.classify(line) == ot.NOISE, line[:70]


def test_real_work_still_needs_triage():
    """The distinction that has to hold: these fired in the same batch and are NOT noise.
    A gym with zero connected platforms publishes nothing until a human acts."""
    for line in (NOT_CONNECTED, KEY_MISMATCH, GBP_PHOTO_FAILED):
        assert ot.classify(line) == ot.NEEDS_TRIAGE, line[:70]


def test_nothing_planned_is_noise_but_a_real_gbp_failure_is_not():
    """Both are GBP, one sentence apart in tone. Only one needs a human."""
    assert ot.classify(GBP_NOTHING_PLANNED) == ot.NOISE
    assert ot.classify(GBP_PHOTO_FAILED) == ot.NEEDS_TRIAGE


# ---- the gate -------------------------------------------------------------------------

class _Poster:
    def __init__(self):
        self.posts = []

    def post_notice(self, text):
        self.posts.append(text)
        return {"ok": True, "ts": "1.0"}

    def _chat_post(self, **kw):
        return {"ok": True}


def _arm(monkeypatch, *, alerts=True, noise_filter=True):
    from agent import config
    monkeypatch.setattr(config, "ops_alerts_enabled", lambda: alerts)
    monkeypatch.setattr(config, "ops_alerts_noise_filter_enabled", lambda: noise_filter)
    monkeypatch.setattr(config, "ops_fix_triage_enabled", lambda: False)


def test_gate_off_posts_everything(monkeypatch):
    """Default state: byte-for-byte today's behaviour."""
    from agent import ops_alerts as oa
    _arm(monkeypatch, noise_filter=False)
    p = _Poster()
    oa.alert(GBP_NOTHING_PLANNED, poster=p)
    assert len(p.posts) == 1


def test_gate_on_drops_noise(monkeypatch):
    from agent import ops_alerts as oa
    _arm(monkeypatch)
    p = _Poster()
    assert oa.alert(GBP_NOTHING_PLANNED, poster=p) is None
    assert p.posts == []


def test_gate_on_still_posts_real_alerts(monkeypatch):
    from agent import ops_alerts as oa
    _arm(monkeypatch)
    p = _Poster()
    oa.alert(NOT_CONNECTED, poster=p)
    oa.alert(GBP_PHOTO_FAILED, poster=p)
    assert len(p.posts) == 2


def test_gate_never_drops_a_forced_alert(monkeypatch):
    """force is for watchdogs carrying their own flag (token watch, listener watch).
    They bypass the master flag and must bypass this too."""
    from agent import ops_alerts as oa
    _arm(monkeypatch, alerts=False)
    p = _Poster()
    oa.alert(GBP_NOTHING_PLANNED, poster=p, force=True)
    assert len(p.posts) == 1


def test_an_unrecognised_shape_is_never_dropped(monkeypatch):
    """The whole safety argument: a new alert call site needs no change here to be heard."""
    from agent import ops_alerts as oa
    _arm(monkeypatch)
    p = _Poster()
    oa.alert("something no call site has ever said before", poster=p)
    assert len(p.posts) == 1


def test_a_classifier_fault_never_eats_an_alert(monkeypatch):
    from agent import ops_alerts as oa
    import agent.ops_triage as triage
    _arm(monkeypatch)
    monkeypatch.setattr(triage, "classify",
                        lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    p = _Poster()
    oa.alert(GBP_NOTHING_PLANNED, poster=p)
    assert len(p.posts) == 1, "a broken classifier must fail toward posting"


def test_a_suppressed_alert_is_still_audited(monkeypatch):
    """Nothing is lost: the audit write happens before every gate."""
    from agent import ops_alerts as oa
    import agent.db as real_db
    _arm(monkeypatch)
    audited = []
    monkeypatch.setattr(real_db, "audit",
                        lambda *a, **kw: audited.append((a, kw)))
    p = _Poster()
    assert oa.alert(GBP_NOTHING_PLANNED, poster=p) is None
    assert p.posts == []
    assert len(audited) == 1, "the suppressed line must still be on the record"


# ---- the interrupted-draw alert answers its own question ------------------------------

def test_interrupted_draw_says_no_action_when_the_day_is_covered(monkeypatch):
    """Three of my own deploys tripped this in one morning while all 18 gyms had a full
    day of rows. Interrupted is not incomplete."""
    from agent import listener as ls
    import agent.db as real_db
    monkeypatch.setattr(ls, "_read_state",
                        lambda: {"draw_started": "2026-09-03", "draw_finished": None})
    monkeypatch.setattr(real_db, "kv_get", lambda k: "")
    monkeypatch.setattr(real_db, "kv_set", lambda k, v: None)
    monkeypatch.setattr(ls, "_gyms_short_on", lambda day: [])
    sent = []
    monkeypatch.setattr(ls.ops_alerts, "alert", lambda m, **kw: sent.append(m))

    assert ls.alert_interrupted_draw() is True
    assert "No action needed" in sent[0]
    # and therefore stays out of Slack once the gate is armed
    assert ot.classify(sent[0]) == ot.NOISE


def test_interrupted_draw_names_the_short_gyms_and_stays_loud(monkeypatch):
    from agent import listener as ls
    import agent.db as real_db
    monkeypatch.setattr(ls, "_read_state",
                        lambda: {"draw_started": "2026-09-03", "draw_finished": None})
    monkeypatch.setattr(real_db, "kv_get", lambda k: "")
    monkeypatch.setattr(real_db, "kv_set", lambda k, v: None)
    monkeypatch.setattr(ls, "_gyms_short_on", lambda day: ["eng", "lasso"])
    sent = []
    monkeypatch.setattr(ls.ops_alerts, "alert", lambda m, **kw: sent.append(m))

    ls.alert_interrupted_draw()
    assert "2 gym(s) have NO rows" in sent[0]
    assert "eng" in sent[0] and "lasso" in sent[0]
    assert "No action needed" not in sent[0]
    assert ot.classify(sent[0]) == ot.NEEDS_TRIAGE


def test_unreadable_coverage_degrades_to_the_loud_branch(monkeypatch):
    """A read failure must never be reported as "everything is fine"."""
    from agent import listener as ls
    import agent.db as real_db
    monkeypatch.setattr(ls, "_read_state",
                        lambda: {"draw_started": "2026-09-03", "draw_finished": None})
    monkeypatch.setattr(real_db, "kv_get", lambda k: "")
    monkeypatch.setattr(real_db, "kv_set", lambda k, v: None)
    monkeypatch.setattr(ls, "_gyms_short_on", lambda day: None)
    sent = []
    monkeypatch.setattr(ls.ops_alerts, "alert", lambda m, **kw: sent.append(m))

    ls.alert_interrupted_draw()
    assert "needs a human look" in sent[0]
    assert "No action needed" not in sent[0]
    assert ot.classify(sent[0]) == ot.NEEDS_TRIAGE


def test_gyms_short_on_returns_none_when_the_store_cannot_be_read(monkeypatch):
    from agent import listener as ls
    import agent.db as real_db
    monkeypatch.setattr(real_db, "gym_list",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))
    assert ls._gyms_short_on("2026-09-03") is None
