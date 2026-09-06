"""AGENT_CTA_VARIETY — the closing-ask fix for Dean Holcomb's CrossFit Reverb
ticket 4941e162-2923-495f-8efb-d2554dea5aec (2026-09-05):

    "All the captions are almost the same as one another. Also, they all end with
     'How do I get started with training at CrossFit Reverb?' which doesn't make
     sense."

Measured on his real forward book (93 rows / 31 posts, 2026-08-31..2026-09-30):
90 of 93 rows (96.8%) ended with that one line, and it was not a CTA at all but an
FAQ HEADING mined out of his own approved source doc, complete with the U+200B it
was pasted in with. `min(candidates, key=len)` preferred the shortest qualifying
sentence, and a heading is shorter than a sentence.

Fleet-wide, top closing line as a share of the book: hillcountry 100%,
train7164ae502 87.5%, theboltonclub 86.2%, gritx 69.9%, topfuel 61.2%, eng 53.9%.

Every test here is deterministic and offline: fake store, injected regen, per-test
sqlite kv (conftest sets AGENT_DB_PATH).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import caption_variety as cv
from agent.jobs import grade_fix


REVERB_CTA = "​How do I get started with training at CrossFit Reverb?"
TODAY = "2026-09-01"


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def patch_pending_plan(self, gym_id, row_id, *, caption=None, pillar=None):
        for r in self.rows:
            if r.get("id") == row_id and r.get("gym_id") == gym_id:
                if str(r.get("status") or "").lower() not in ("pending", "draft", "queued"):
                    return None
                if caption is not None:
                    r["caption"] = caption
                if pillar is not None:
                    r["pillar"] = pillar
                r["status"] = "pending"
                return dict(r)
        return None


def _no_ask_caption(i, *, vary_body=True):
    """A caption with NO ask, long enough to clear the 150 char floor. This is the
    shape that held five of seven live gyms at C or worse.

    vary_body mirrors reality: Reverb's 31 bodies WERE genuinely different (30 of
    31 distinct closings once the stapled CTA is removed). Set it False to model a
    book whose bodies are formulaic, which is a different defect (see
    test_identical_bodies_still_collide_and_the_cta_layer_cannot_fix_that)."""
    tail = (f"That is how beginner number {i} turns into a regular."
            if vary_body else "That is how a beginner turns into a regular.")
    return (f"Day {i} at the gym and the rig is full again.\n\n"
            "Coaches walk every member through the movement before a single rep "
            f"counts, so nobody is left guessing what good looks like today. {tail}")


def _book(n=12, gym_id="reverb", vary_body=True):
    return [{"id": f"row_{i}", "gym_id": gym_id,
             "post_date": f"2026-09-{i + 1:02d}",
             "caption": _no_ask_caption(i, vary_body=vary_body),
             "pillar": "community",
             "status": "pending", "account": "instagram", "format": "feed"}
            for i in range(n)]


POOL = ["Book your free No Sweat Intro",
        "Send us a message to get started",
        "Visit crossfitreverb.com to book your first visit",
        "Schedule a free intro"]


def _run(rows, monkeypatch, *, variety, pool=None, cta=None):
    if variety:
        monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    else:
        monkeypatch.delenv("AGENT_CTA_VARIETY", raising=False)
    if pool is not None:
        monkeypatch.setattr(grade_fix, "_booking_cta_pool",
                            lambda gym_id, log: list(pool))
    store = _FakeStore(rows)
    grade_fix._fix_craft("reverb", rows, store, "GYM", None, set(),
                         lambda m: None, booking_cta=cta)
    return rows


def _closings(rows):
    return [cv.closing_signature(r["caption"]) for r in rows]


# ---------------------------------------------------------------------------
# 1. The flag OFF path is byte-for-byte the old behavior (regression guard)
# ---------------------------------------------------------------------------

def test_flag_off_still_staples_one_cta_onto_every_day(monkeypatch):
    """OFF by default = zero behavior change. This test documents the DEFECT and
    exists so arming the flag is provably what changes things."""
    rows = _run(_book(8), monkeypatch, variety=False, cta="Book your free intro")
    tails = _closings(rows)
    assert len(set(tails)) == 1, tails
    assert all(t == "book your free intro" for t in tails)


def test_flag_off_still_accepts_a_question_as_a_cta(monkeypatch):
    """The old gate only asked 'does this contain an ask phrase'. Unflagged, it
    still does, so an existing gym's book cannot shift under it."""
    rows = _run(_book(4), monkeypatch, variety=False, cta=REVERB_CTA)
    assert all("How do I get started" in r["caption"] for r in rows)


# ---------------------------------------------------------------------------
# 2. Armed: the question is rejected
# ---------------------------------------------------------------------------

def test_armed_rejects_the_reverb_heading_and_appends_nothing(monkeypatch):
    """Reverb's approved sources contain exactly one 'booking ask' and it is a
    heading. Once it is rejected the pool is EMPTY, and an empty pool must give
    an honest skip, never an invented CTA."""
    logged = []
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: [])
    rows = _book(6)
    grade_fix._fix_craft("reverb", rows, _FakeStore(rows), "GYM", None, set(),
                         logged.append, booking_cta=None)
    assert all("get started with training" not in r["caption"] for r in rows)
    assert all("​" not in r["caption"] for r in rows)


def test_pool_filters_out_a_heading_but_keeps_real_ctas(monkeypatch, tmp_path):
    """_booking_cta_pool must drop the question and keep the imperative, and say
    in the log which candidate it dropped and why."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    from agent import client_sources as cs
    cs.add_source("reverb_ig", "offer", REVERB_CTA, "source doc")
    cs.add_source("reverb_ig", "offer", "Book your free intro class today.", "source doc")
    logs = []
    pool = grade_fix._booking_cta_pool("reverb", logs.append)
    assert pool == ["Book your free intro class today."]
    assert any("cta_is_question" in m for m in logs), logs


def test_pool_unarmed_keeps_the_heading(monkeypatch, tmp_path):
    """The mutation guard for the filter: with the flag off the heading is still
    in the pool, so the filter is provably what removes it."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.delenv("AGENT_CTA_VARIETY", raising=False)
    from agent import client_sources as cs
    cs.add_source("reverb_ig", "offer", REVERB_CTA, "source doc")
    pool = grade_fix._booking_cta_pool("reverb", lambda m: None)
    assert pool and "get started with training" in pool[0]


# ---------------------------------------------------------------------------
# 3. Armed: rotation, so no two posts in the window share a closing
# ---------------------------------------------------------------------------

def test_armed_rotates_the_pool_instead_of_stapling_one_line(monkeypatch):
    rows = _run(_book(4), monkeypatch, variety=True, pool=POOL)
    tails = [t for t in _closings(rows) if t]
    assert len(set(tails)) == len(tails), tails
    assert len(set(tails)) > 1


def test_armed_leaves_no_closing_collision_inside_the_window(monkeypatch):
    """THE guarantee, asserted the same way Part A measured the defect."""
    rows = _run(_book(20), monkeypatch, variety=True, pool=POOL)
    report = cv.report(rows, window=10)
    assert [c for c in report["collisions"] if c["kind"] == "closing"] == []


def test_armed_beats_the_measured_before_numbers(monkeypatch):
    """Before, on Reverb's real book: 2 distinct closings across 31 posts and a
    top closing share of 96.8%. Armed, on a book of the same size, the top
    closing share must be a small fraction of the book."""
    rows = _run(_book(31), monkeypatch, variety=True, pool=POOL)
    report = cv.report(rows, window=10)
    assert report["posts"] == 31
    assert report["top_share"]["closing"] < 0.25, report["top_share"]
    assert report["distinct"]["closing"] >= 4, report["distinct"]


def test_identical_bodies_still_collide_and_the_cta_layer_cannot_fix_that():
    """THE HONEST LIMIT of this fix, stated as a test rather than a footnote.

    The CTA layer guarantees no two posts share an APPENDED ASK inside the
    window. It cannot make two posts different when their BODIES are identical:
    an ask-less post's closing line is its body's last line. Body variety is a
    separate lever (per-post differentiating context in the drafter). This test
    exists so nobody reads the closing-collision guarantee as broader than it is.
    """
    rows = _book(8, vary_body=False)
    report = cv.report(rows, window=10)
    assert report["distinct"]["closing"] == 1
    assert [c for c in report["collisions"] if c["kind"] == "closing"]


# ---------------------------------------------------------------------------
# 4. Armed: not every post ends in an ask
# ---------------------------------------------------------------------------

def test_armed_stops_appending_once_the_book_has_its_ask_floor(monkeypatch):
    """Dean is right that an ask on every post reads wrong. The booking CTA is
    appended only while the book is short of the grader's floor of 5 booking
    asks; after that a repaired caption legitimately carries none."""
    rows = _run(_book(20), monkeypatch, variety=True, pool=POOL)
    pool_sigs = {cv.normalize(c) for c in POOL}
    appended = [r for r in rows if cv.closing_signature(r["caption"]) in pool_sigs]
    # The grader's GYM floor is min(5, n) booking asks. Appending stops there:
    # 5 of 20 posts, not 20 of 20. Before this gate every craft-flagged day got
    # the CTA, which is how one line reached 90 of Reverb's 93 rows.
    assert len(appended) == 5, f"{len(appended)} of {len(rows)}"
    assert len(appended) < len(rows), "an ask on every post is the defect"
    # A CTA may only recur once it is OUTSIDE the window, never inside it.
    assert [c for c in cv.collisions(rows, window=10) if c["kind"] == "closing"] == []


def test_ask_free_caption_can_pass_the_craft_bar_only_when_allowed(monkeypatch):
    """`no_ask` in soft_flags made 'every post ends in an ask' structurally
    unavoidable. allow_no_ask is what lifts it, and only that."""
    clean_no_ask = _no_ask_caption(3)
    assert grade_fix._clears_craft(clean_no_ask) is False
    assert grade_fix._clears_craft(clean_no_ask, allow_no_ask=True) is True


def test_allow_no_ask_does_not_lower_any_other_bar():
    """It lifts exactly one flag. A thin or hook-broken caption still fails."""
    assert grade_fix._clears_craft("Too short.", allow_no_ask=True) is False
    long_hook = "x " * 90
    assert grade_fix._clears_craft(long_hook + "\nbody " * 20,
                                   allow_no_ask=True) is False


# ---------------------------------------------------------------------------
# 5. Nothing is ever invented
# ---------------------------------------------------------------------------

def test_every_appended_cta_came_from_the_pool(monkeypatch):
    """The hard rule: client content only. Every closing line in the repaired
    book must be a string the caller handed in."""
    rows = _run(_book(12), monkeypatch, variety=True, pool=POOL)
    allowed = {cv.normalize(c) for c in POOL}
    for r in rows:
        tail = cv.closing_signature(r["caption"])
        body_tail = cv.normalize(_no_ask_caption(0).splitlines()[-1])
        if tail and tail != body_tail:
            assert tail in allowed or tail.startswith("coaches walk"), r["caption"]


def test_empty_pool_never_produces_an_ask(monkeypatch):
    rows = _run(_book(6), monkeypatch, variety=True, pool=[])
    from agent.calendar_grade import _BOOKING_RE
    assert [r for r in rows if _BOOKING_RE.search(r["caption"])] == []


# ---------------------------------------------------------------------------
# 6. The learning levers must tell the truth after a repair
# ---------------------------------------------------------------------------
# Measured on Reverb's live book: ask_type='none' on 93 of 93 rows while 90 of
# them ended in an ask, and caption_len_band='mid' on 100% of rows. The levers
# are stamped at STAGE time against the SB7 body (which by design carries no
# CTA); this lane then mutates the caption and only ever wrote caption/pillar.
# jobs/backfill_levers could not correct it either: it selects rows WHERE
# hook_family IS NULL, and these were already stamped. metrics_sync copies these
# columns onto post_metrics and monthly_retro compares on them, so a stale lever
# is a lie the cross-gym learner trains on.

class _LeverStore(_FakeStore):
    def __init__(self, rows):
        super().__init__(rows)
        self.levers_seen = []

    def patch_pending_plan(self, gym_id, row_id, *, caption=None, pillar=None,
                           levers=None):
        self.levers_seen.append(levers)
        out = super().patch_pending_plan(gym_id, row_id, caption=caption,
                                         pillar=pillar)
        if out is not None and levers:
            for r in self.rows:
                if r.get("id") == row_id:
                    r.update(levers)
        return out


def test_repair_restamps_ask_type_to_the_truth(monkeypatch):
    """The exact production lie: a caption that ends in an ask labelled
    ask_type='none'."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: list(POOL))
    rows = _book(4)
    for r in rows:
        r["ask_type"] = "none"
        r["caption_len_band"] = "mid"
    grade_fix._fix_craft("reverb", rows, _LeverStore(rows), "GYM", None, set(),
                         lambda m: None, booking_cta=None)
    from agent.calendar_grade import _BOOKING_RE
    for r in rows:
        if _BOOKING_RE.search(r["caption"]):
            assert r["ask_type"] != "none", r


def test_restamped_levers_match_what_lever_stamp_says(monkeypatch):
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: list(POOL))
    rows = _book(3)
    store = _LeverStore(rows)
    grade_fix._fix_craft("reverb", rows, store, "GYM", None, set(),
                         lambda m: None, booking_cta=None)
    from agent import lever_stamp
    for r in rows:
        assert r["ask_type"] == lever_stamp.ask_type(r["caption"])
        assert r["caption_len_band"] == lever_stamp.caption_len_band(r["caption"])
        assert r["hook_family"] == lever_stamp.hook_family(r["caption"])


def test_levers_are_not_restamped_when_the_flag_is_off(monkeypatch):
    """OFF by default = zero behavior change, metadata included."""
    monkeypatch.delenv("AGENT_CTA_VARIETY", raising=False)
    rows = _book(3)
    store = _LeverStore(rows)
    grade_fix._fix_craft("reverb", rows, store, "GYM", None, set(),
                         lambda m: None, booking_cta="Book your free intro")
    assert all(l in (None, {}) for l in store.levers_seen), store.levers_seen


def test_a_store_without_the_levers_kwarg_still_gets_its_caption_fixed(monkeypatch):
    """A metadata refresh must never cost a caption fix. Older stores and fakes
    take caption/pillar only."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: list(POOL))
    rows = _book(3)
    before = [r["caption"] for r in rows]
    grade_fix._fix_craft("reverb", rows, _FakeStore(rows), "GYM", None, set(),
                         lambda m: None, booking_cta=None)
    assert [r["caption"] for r in rows] != before


def test_patch_pending_plan_only_writes_allowlisted_lever_columns():
    """The levers kwarg must not become a way to write an arbitrary column."""
    from agent import portal_calendar_store as pcs
    assert pcs._LEVER_COLUMNS == ("hook_family", "ask_type", "caption_len_band")

    sent = {}

    class _Http:
        def patch(self, url, params=None, headers=None, json=None, timeout=None):
            sent.update(json or {})

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return [{"gym_id": "reverb", "id": "row_1"}]
            return _R()

    store = pcs.SupabaseCalendarStore(url="https://x", service_key="k", http=_Http())
    store.patch_pending_plan("reverb", "row_1", caption="c",
                             levers={"ask_type": "booking_link",
                                     "status": "approved",
                                     "gym_id": "someone_else"})
    assert sent["ask_type"] == "booking_link"
    assert "gym_id" not in sent, "a non-lever column must never be written here"
    assert sent["status"] == "pending", "the approval gate is never weakened"


def test_lever_stamp_reads_a_real_booking_cta_as_an_ask():
    """The SECOND, independent reason ask_type said 'none' on 93 of 93 Reverb
    rows. copy_gate.ASK_RE was fixed for this exact adjacency false negative on
    2026-08-31 ("Book a FREE NO SWEAT intro"); lever_stamp never got the fix, so
    the classifier read every real booking CTA as no ask at all."""
    from agent import lever_stamp
    for cta in ("Book your free No Sweat Intro",
                "Book a free no sweat intro",
                "Book your first class",
                "Book a free intro session"):
        assert lever_stamp.ask_type(f"Body copy here.\n{cta}") == "booking_link", cta


def test_lever_stamp_still_reports_none_when_there_is_no_ask():
    from agent import lever_stamp
    assert lever_stamp.ask_type("We had a great week at the gym.") == "none"


def test_lever_stamp_does_not_swallow_a_whole_sentence_as_an_ask():
    """Up to three modifier words, the same bound copy_gate uses. More than that
    is a sentence, not an ask."""
    from agent import lever_stamp
    assert lever_stamp.ask_type(
        "Book your very best most incredibly memorable first class") == "none"
