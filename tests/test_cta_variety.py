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

from agent import calendar_grade, caption_variety as cv, copy_gate
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

def test_armed_stops_appending_once_the_book_has_its_ask_target(monkeypatch):
    """Dean is right that an ask on every post reads wrong. The booking CTA is
    appended only while the book is short of its ask TARGET; after that a
    repaired caption legitimately carries none.

    The target was `min(5, n)` when this test was written (5 of 20). Blake moved
    it to a 33% share on 2026-09-06, so 20 posts now want 7. The invariant this
    test exists for is unchanged and is the second assertion: appending STOPS
    somewhere well short of the whole book."""
    rows = _run(_book(20), monkeypatch, variety=True, pool=POOL)
    pool_sigs = {cv.normalize(c) for c in POOL}
    appended = [r for r in rows if cv.closing_signature(r["caption"]) in pool_sigs]
    # 20 posts * 0.33 = 6.6 -> 7. Before this gate every craft-flagged day got
    # the CTA, which is how one line reached 90 of Reverb's 93 rows.
    assert len(appended) == 7, f"{len(appended)} of {len(rows)}"
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


def test_local_rows_carry_the_new_levers_before_any_store_re_read(monkeypatch):
    """grade_fix regrades the book from the IN-MEMORY rows straight after a
    repair, so the local dicts have to reflect what was written, exactly as the
    caption line beside it already did. A store that accepts the levers kwarg but
    echoes nothing back proves the local update is doing the work, not the fake.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: list(POOL))

    class _SilentStore(_FakeStore):
        def patch_pending_plan(self, gym_id, row_id, *, caption=None, pillar=None,
                               levers=None):
            # accepts levers, deliberately writes none back
            return super().patch_pending_plan(gym_id, row_id, caption=caption,
                                              pillar=pillar)

    rows = _book(3)
    for r in rows:
        r["ask_type"] = "none"
    grade_fix._fix_craft("reverb", rows, _SilentStore(rows), "GYM", None, set(),
                         lambda m: None, booking_cta=None)
    from agent import lever_stamp
    assert any(r["ask_type"] != "none" for r in rows), rows
    for r in rows:
        assert r["ask_type"] == lever_stamp.ask_type(r["caption"])


# ---------------------------------------------------------------------------
# THE 33% ASK RATE (Blake, 2026-09-06: "every post should not have an ask, make
# it 33% of post"). The shipped target was `min(5, n)` -- 5 of 31 posts, 16.1%.
#
# Two halves, and the second one is the point: moving the REPAIR without moving
# the GRADER would build a system that fights itself, because the grader's rule
# was "every post carries an ask" and it was scored on TWO legs at once.
# ---------------------------------------------------------------------------

def test_ask_target_is_a_share_of_the_book_not_a_flat_five(monkeypatch):
    """31 posts at 0.33 wants 10, not 5. The old flat floor is what produced
    16.1% on Dean's real book."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    assert grade_fix._ask_target_posts(31) == 10
    assert grade_fix._ask_target_posts(30) == 10
    assert grade_fix._ask_target_posts(0) == 0


def test_the_graders_booking_floor_still_binds_on_a_short_book(monkeypatch):
    """A share below the grader's own `min(5, n)` floor would hand the repair a
    target the grader still marks down. The larger of the two wins."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    assert grade_fix._ask_target_posts(6) == 5      # share would be 2
    assert grade_fix._ask_target_posts(3) == 3      # never more posts than exist


def test_the_target_is_configurable_and_refuses_nonsense(monkeypatch):
    from agent import config
    monkeypatch.setenv("AGENT_CAPTION_ASK_RATE", "0.5")
    assert config.caption_ask_rate_target() == 0.5
    for bad in ("0", "-1", "1.5", "banana", ""):
        monkeypatch.setenv("AGENT_CAPTION_ASK_RATE", bad)
        assert config.caption_ask_rate_target() == 0.33, bad


def test_the_deficit_counts_posts_not_rows(monkeypatch):
    """One post spans several rows (IG feed + FB mirror + paired story share one
    caption on one date). A share taken over ROWS asks for 31 posts' worth of CTA
    on a 31 post book, because the caller decrements the deficit once per POST.
    Reverb's book is 93 rows and 31 posts, which is exactly the 3x that hides it.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = []
    for i in range(30):
        cap = _no_ask_caption(i)
        for plat in ("instagram", "facebook", "story"):
            rows.append({"id": f"r{i}_{plat}", "gym_id": "reverb",
                         "post_date": f"2026-09-{i + 1:02d}", "caption": cap,
                         "status": "pending", "account": plat, "format": "feed"})
    assert len(rows) == 90
    # 30 POSTS at 0.33 -> 10. Counting the 90 ROWS would give 30.
    assert grade_fix._booking_deficit(rows) == 10


def test_flag_off_keeps_the_flat_five_row_deficit(monkeypatch):
    monkeypatch.delenv("AGENT_CTA_VARIETY", raising=False)
    rows = _book(30)
    assert grade_fix._booking_deficit(rows) == 5


def test_repairing_a_no_ask_book_lands_near_the_target(monkeypatch):
    """END TO END on the real repair loop: a 31 post book of ask-less captions
    comes out of _fix_craft with ~a third of its posts asking, not a twentieth
    and not all of them."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: list(POOL))
    rows = _book(31)
    grade_fix._fix_craft("reverb", rows, _FakeStore(rows), "GYM", None, set(),
                         lambda m: None)
    asking = [r for r in rows if copy_gate.ASK_RE.search(r["caption"])]
    assert len(asking) == 10, [r["caption"][-60:] for r in asking]
    assert 0.28 <= len(asking) / len(rows) <= 0.38


def test_the_grader_does_not_mark_down_a_book_that_hits_the_target(monkeypatch):
    """THE RECONCILIATION. Measured on Dean's real book before this: a book
    repaired exactly as intended scored 71 (C) with 25 'no ask' defects, because
    the grader still wanted an ask on every post AND counted `no_ask` twice --
    once on path_to_join (up to 7) and again as a caption_craft soft flag (up to
    12). Both legs now agree with the target."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: list(POOL))
    rows = _book(31)
    grade_fix._fix_craft("reverb", rows, _FakeStore(rows), "GYM", None, set(),
                         lambda m: None)
    grade = calendar_grade.grade_month(rows, profile="GYM")
    no_ask = [d for d in grade.defects if "no ask in caption" in str(d[2])]
    assert no_ask == [], no_ask
    assert grade.scores["path_to_join"] == 10, grade.scores
    soft_no_ask = [d for d in grade.defects if "soft flag: no_ask" in str(d[2])]
    assert soft_no_ask == [], soft_no_ask


def test_a_book_with_no_asks_at_all_still_loses_the_full_penalty(monkeypatch):
    """The band must not become a licence to never ask. 0% keeps the same worst
    case path_to_join always had.

    The gym must HAVE a usable CTA pool for this rule to apply at all: a gym
    with nothing approved to ask with is exempt by design (Blake, 2026-09-06),
    which is the test immediately below."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: list(POOL))
    rows = _book(31)
    grade = calendar_grade.grade_month(rows, profile="GYM")
    # 7 for the ask rule + 5 for the booking-specific leg takes the leg to 0.
    assert grade.scores["path_to_join"] == 0, grade.scores
    no_ask = [d for d in grade.defects if "no ask in caption" in str(d[2])]
    assert len(no_ask) == 10, len(no_ask)   # the SHORTFALL, not all 31


def test_flag_off_the_grader_still_wants_an_ask_on_every_post(monkeypatch):
    """Regression guard: unarmed gyms see byte-for-byte the old rule."""
    monkeypatch.delenv("AGENT_CTA_VARIETY", raising=False)
    rows = _book(31)
    grade = calendar_grade.grade_month(rows, profile="GYM")
    no_ask = [d for d in grade.defects if "no ask in caption" in str(d[2])]
    assert len(no_ask) == 31, len(no_ask)
    soft_no_ask = [d for d in grade.defects if "soft flag: no_ask" in str(d[2])]
    assert len(soft_no_ask) == 31, len(soft_no_ask)


# ---------------------------------------------------------------------------
# THE OTHER HALF OF 33%: removing the staple.
#
# _fix_craft can only ADD an ask, and only to a craft-FLAGGED day. A caption that
# already ends in an ask is not flagged, so on Dean's live book -- 30 of 31 posts
# closing on the same line, exactly ONE post craft-flagged -- the repair loop had
# nothing to grip and his actual complaint stayed true. This pass removes the
# staple. It only ever DELETES a line the machine stapled on.
# ---------------------------------------------------------------------------

def _stapled_book(n=31, staple=REVERB_CTA, gym_id="reverb"):
    """A book shaped like Dean's: every post closing on the same line."""
    rows = []
    for i in range(n):
        cap = f"{_no_ask_caption(i)}\n{staple}"
        rows.append({"id": f"row_{i}", "gym_id": gym_id,
                     "post_date": f"2026-09-{i + 1:02d}", "caption": cap,
                     "pillar": "community", "status": "pending",
                     "account": "instagram", "format": "feed"})
    return rows


def test_the_staple_is_trimmed_down_to_the_target(monkeypatch):
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _stapled_book(31)
    before = sum(1 for r in rows if copy_gate.ASK_RE.search(r["caption"]))
    assert before == 31, before
    grade_fix._fix_ask_excess("reverb", rows, _FakeStore(rows), lambda m: None)
    after = sum(1 for r in rows if copy_gate.ASK_RE.search(r["caption"]))
    # 31 posts * 0.33 -> 10. Trimming stops there and never goes below.
    assert after == 10, after
    assert 0.28 <= after / len(rows) <= 0.38


def test_trimming_never_takes_a_book_below_the_target(monkeypatch):
    """The pass must not be able to strip a book that is already compliant."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _stapled_book(31)
    grade_fix._fix_ask_excess("reverb", rows, _FakeStore(rows), lambda m: None)
    first = sum(1 for r in rows if copy_gate.ASK_RE.search(r["caption"]))
    # Run it again: an at-target book is left completely alone.
    trimmed = grade_fix._fix_ask_excess("reverb", rows, _FakeStore(rows), lambda m: None)
    assert trimmed == 0
    assert sum(1 for r in rows if copy_gate.ASK_RE.search(r["caption"])) == first


class _PermissiveStore(_FakeStore):
    """Writes whatever it is handed, including onto a non-pending row.

    _FakeStore refuses a non-pending row itself, which MASKS the caller's own
    human-owned guard: revert the guard in _fix_ask_excess and the fake still
    blocks the write, so the test stays green and asserts nothing. Mutation
    checking caught exactly that. These guard tests use a store that protects
    nothing, so only the code under test can hold the line.
    """

    def patch_pending_plan(self, gym_id, row_id, *, caption=None, pillar=None,
                           levers=None):
        for r in self.rows:
            if r.get("id") == row_id and r.get("gym_id") == gym_id:
                if caption is not None:
                    r["caption"] = caption
                return dict(r)
        return None


def test_a_bespoke_closing_that_appears_once_is_never_trimmed(monkeypatch):
    """A sign-off used on ONE post is the gym's own voice, not a staple.

    Every post here closes on a DIFFERENT ask, so the repeated-closing guard is
    the only thing standing between the pass and 21 trims. If it is reverted the
    book is gutted.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _stapled_book(31)
    for i, r in enumerate(rows):
        r["caption"] = f"{_no_ask_caption(i)}\nBook your free intro on day {i} now"
    assert all(copy_gate.ASK_RE.search(r["caption"]) for r in rows)
    trimmed = grade_fix._fix_ask_excess("reverb", rows, _PermissiveStore(rows),
                                        lambda m: None)
    assert trimmed == 0, trimmed
    assert all("Book your free intro on day" in r["caption"] for r in rows)


def test_an_ask_woven_into_the_body_is_never_trimmed(monkeypatch):
    """Only the LAST line is ever removed, and only when it is itself an ask.

    Every post here carries its ask in the FIRST line and closes on a shared,
    heavily repeated line that is NOT an ask. The repeated-closing guard is
    therefore satisfied and the is-it-an-ask guard is the only one left; revert
    it and the pass eats 21 closing lines that were never CTAs.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    closing = "That is how a beginner turns into a regular here every week."
    rows = _stapled_book(31)
    for i, r in enumerate(rows):
        r["caption"] = (f"Book your free intro and the rest sorts itself out.\n\n"
                        f"{_no_ask_caption(i)}\n{closing}")
    assert all(copy_gate.ASK_RE.search(r["caption"]) for r in rows)
    assert not copy_gate.ASK_RE.search(closing)
    trimmed = grade_fix._fix_ask_excess("reverb", rows, _PermissiveStore(rows),
                                        lambda m: None)
    assert trimmed == 0, trimmed
    assert all(r["caption"].rstrip().endswith(closing) for r in rows)


def test_a_human_owned_day_is_never_trimmed(monkeypatch):
    """EVERY post is human-owned and the store protects nothing.

    HONEST NOTE, because mutation checking made it explicit: the `_is_wipeable`
    line in _fix_ask_excess is DEFENCE IN DEPTH, not the sole guard --
    _patch_date_rows enforces the same rule underneath it, so reverting EITHER
    layer alone leaves this test green. Reverting BOTH turns it red (verified).
    What this test pins is therefore the RULE, "a human-owned day is never
    trimmed", not either individual line. Said out loud so nobody later reads a
    green suite as proof that one of the two lines is load-bearing on its own.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _stapled_book(31)
    for r in rows:
        r["status"] = "approved"
    before = [r["caption"] for r in rows]
    trimmed = grade_fix._fix_ask_excess("reverb", rows, _PermissiveStore(rows),
                                        lambda m: None)
    assert trimmed == 0, trimmed
    assert [r["caption"] for r in rows] == before


def test_remediate_forward_book_removes_an_invalid_closing(monkeypatch):
    """THE WIRING, and the order. A pass that is built and never called is the
    failure this repo keeps repeating.

    Reverb's staple is not a valid CTA (it is a question), so the INVALID pass
    owns it and the trim pass never sees it -- which is the point of running
    the invalid pass first."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: [])
    rows = _stapled_book(31)
    out = grade_fix.remediate_forward_book(
        "reverb", rows, _FakeStore(rows), profile="GYM", defects=[],
        today_iso=TODAY, caption_regen=lambda *a, **k: None,
        gap_filler=lambda *a, **k: "none", logger=lambda m: None)
    assert out["invalid_closings_removed"] == 31, out
    assert any("not a valid CTA" in a for a in out["actions"]), out["actions"]
    assert not any(REVERB_CTA.strip() in r["caption"] for r in rows)


def test_remediate_forward_book_actually_runs_the_trim_pass(monkeypatch):
    """THE OTHER WIRING. A VALID CTA repeated on every post is the trim pass's
    job, not the invalid pass's, and it must be trimmed back to target."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: [])
    valid = "Book your free No Sweat Intro"
    assert copy_gate.is_cta_shaped(valid)
    rows = _stapled_book(31, staple=valid)
    out = grade_fix.remediate_forward_book(
        "reverb", rows, _FakeStore(rows), profile="GYM", defects=[],
        today_iso=TODAY, caption_regen=lambda *a, **k: None,
        gap_filler=lambda *a, **k: "none", logger=lambda m: None)
    assert out["invalid_closings_removed"] == 0, out
    assert out["ask_trimmed"] > 0, out
    assert any("trimmed the stapled closing ask" in a for a in out["actions"]), out["actions"]
    asking = sum(1 for r in rows if copy_gate.ASK_RE.search(r["caption"]))
    assert asking == 10, asking


def test_the_trim_pass_is_inert_with_the_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_CTA_VARIETY", raising=False)
    rows = _stapled_book(31)
    before = [r["caption"] for r in rows]
    assert grade_fix._fix_ask_excess("reverb", rows, _FakeStore(rows), lambda m: None) == 0
    assert [r["caption"] for r in rows] == before


def test_trimming_breaks_up_the_closing_monoculture(monkeypatch):
    """The point of the pass, measured the way Dean's complaint was measured."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _stapled_book(31)
    before = cv.report(rows)
    assert before["distinct"]["closing"] == 1, before["distinct"]
    grade_fix._fix_ask_excess("reverb", rows, _FakeStore(rows), lambda m: None)
    after = cv.report(rows)
    assert after["distinct"]["closing"] > before["distinct"]["closing"]
    assert after["top_share"]["closing"] < before["top_share"]["closing"]
    assert len(after["collisions"]) < len(before["collisions"])


def test_a_trim_that_would_leave_a_worse_caption_is_skipped(monkeypatch):
    """The remainder still has to clear the craft bar, same as every other repair.

    EVERY post here is too short to survive on its own, so the craft re-check is
    the ONLY guard in play: revert it and all 21 excess posts get gutted down to
    a one line caption under the 150 char floor. An earlier version of this test
    left only four such posts, and those four ranked last by closing frequency,
    so the excess was exhausted before the pass ever reached them and reverting
    the guard changed nothing. Mutation checking caught that.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _stapled_book(31)
    for r in rows:
        r["caption"] = f"Short line.\n{REVERB_CTA}"
    trimmed = grade_fix._fix_ask_excess("reverb", rows, _PermissiveStore(rows),
                                        lambda m: None)
    assert trimmed == 0, trimmed
    assert all(REVERB_CTA in r["caption"] for r in rows)


# ---------------------------------------------------------------------------
# A LIVE REGRESSION, unrelated to the ask rate but in the same file.
#
# The TypeError retry in _patch_date_rows sat BARE inside its own handler, so a
# store that raised on the retry aborted the ENTIRE gym's grade-fix pass. Before
# the levers change one broad `except Exception` caught every store error and
# moved to the next row; splitting TypeError out silently removed that
# protection from the path most likely to fail.
# ---------------------------------------------------------------------------

def test_one_failing_row_does_not_abort_the_whole_pass(monkeypatch):
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")

    class _RetryBombStore:
        """Rejects the levers kwarg (forcing the retry) and then blows up on the
        retry for the first row only. Row two must still be patched."""

        def __init__(self, rows):
            self.rows = rows
            self.patched = []

        def patch_pending_plan(self, gym_id, row_id, *, caption=None,
                               pillar=None, levers=None):
            if levers is not None:
                raise TypeError("this store predates the levers kwarg")
            if row_id == "row_0":
                raise RuntimeError("supabase 503")
            self.patched.append(row_id)
            for r in self.rows:
                if r.get("id") == row_id:
                    r["caption"] = caption
                    return dict(r)
            return None

    # 31 posts so the book is genuinely ABOVE target and the pass has work to do
    # (a 4 post book's target is 4, so nothing would be trimmed and the test
    # would pass while asserting nothing).
    rows = _stapled_book(31)
    store = _RetryBombStore(rows)
    # Must not raise, and must get past the exploding row.
    grade_fix._fix_ask_excess("reverb", rows, store, lambda m: None)
    assert store.patched, "the pass aborted on the first failing row"
    assert "row_0" not in store.patched


# ---------------------------------------------------------------------------
# BLAKE'S RULING, 2026-09-06: "If a gym's CTA pool is empty or too thin to
# supply a real ask, DO NOT force one in. No fake/generic/repeated CTA just to
# hit a target. On those gyms, write the caption with no booking ask at all --
# good copy, no ask -- rather than degrade quality or repeat the same CTA to
# hit 33%."
#
# That corrected this module's first answer. _fix_ask_excess trims a book DOWN
# TO the target, which on Dean's book meant KEEPING his nonsensical FAQ heading
# on ten posts in order to reach 33%. A line that is not a valid CTA is not a
# partial ask to be rationed -- it is wrong copy, and it goes from every post.
# ---------------------------------------------------------------------------

def test_an_invalid_closing_is_removed_from_every_post_not_rationed(monkeypatch):
    """Dean's exact case. His line is a question, so it is not a CTA at all and
    the ask rate is irrelevant to whether it stays."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    assert not copy_gate.is_cta_shaped(REVERB_CTA)
    rows = _stapled_book(31)
    removed = grade_fix._fix_invalid_closings("reverb", rows,
                                              _PermissiveStore(rows), lambda m: None)
    assert removed == 31, removed
    assert not any("get started with training" in r["caption"] for r in rows)
    # And NOT rationed down to the 33% target: none survive.
    assert sum(1 for r in rows if copy_gate.ASK_RE.search(r["caption"])) == 0


def test_a_valid_cta_is_never_removed_by_the_invalid_pass(monkeypatch):
    """The rate rules own a real CTA; this pass must not touch one."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    valid = "Book your free No Sweat Intro"
    assert copy_gate.is_cta_shaped(valid)
    rows = _stapled_book(31, staple=valid)
    removed = grade_fix._fix_invalid_closings("reverb", rows,
                                              _PermissiveStore(rows), lambda m: None)
    assert removed == 0, removed
    assert all(valid in r["caption"] for r in rows)


def test_the_invalid_pass_never_touches_ordinary_body_copy(monkeypatch):
    """A closing line that is not read as an ask at all is left alone, even
    though it is also not CTA-shaped."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    closing = "That is how a beginner turns into a regular here every week."
    assert not copy_gate.ASK_RE.search(closing)
    rows = _stapled_book(31)
    for i, r in enumerate(rows):
        r["caption"] = f"{_no_ask_caption(i)}\n{closing}"
    assert grade_fix._fix_invalid_closings("reverb", rows, _PermissiveStore(rows),
                                           lambda m: None) == 0
    assert all(r["caption"].rstrip().endswith(closing) for r in rows)


def test_the_invalid_pass_never_touches_a_human_owned_day(monkeypatch):
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _stapled_book(31)
    for r in rows:
        r["status"] = "approved"
    before = [r["caption"] for r in rows]
    assert grade_fix._fix_invalid_closings("reverb", rows, _PermissiveStore(rows),
                                           lambda m: None) == 0
    assert [r["caption"] for r in rows] == before


def test_the_invalid_pass_is_inert_with_the_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_CTA_VARIETY", raising=False)
    rows = _stapled_book(31)
    before = [r["caption"] for r in rows]
    assert grade_fix._fix_invalid_closings("reverb", rows, _PermissiveStore(rows),
                                           lambda m: None) == 0
    assert [r["caption"] for r in rows] == before


def test_the_invalid_pass_skips_a_removal_that_leaves_a_worse_caption(monkeypatch):
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _stapled_book(31)
    for r in rows:
        r["caption"] = f"Short line.\n{REVERB_CTA}"
    assert grade_fix._fix_invalid_closings("reverb", rows, _PermissiveStore(rows),
                                           lambda m: None) == 0
    assert all(REVERB_CTA.strip() in r["caption"] for r in rows)


# --- the grader half of the same ruling -------------------------------------

def test_a_gym_with_no_usable_cta_is_exempt_from_both_ask_rules(monkeypatch):
    """A gym told not to force an ask must not then be marked down for not
    asking. Without this, obeying Blake's rule costs Dean's book 11 points and
    grade_sweep re-attempts the same unfixable days every night forever."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: [])
    rows = _book(31)                       # no asks anywhere
    grade = calendar_grade.grade_month(rows, profile="GYM")
    assert grade.scores["path_to_join"] == 10, grade.scores
    assert any("no approved CTA" in k for k in grade.exempt), grade.exempt
    assert [d for d in grade.defects if d[0] == "path_to_join"] == []


def test_a_gym_that_HAS_a_cta_is_still_held_to_both_ask_rules(monkeypatch):
    """The mutation guard for the exemption: same ask-less book, but this gym
    has something to ask with, so it is scored exactly as before."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: list(POOL))
    rows = _book(31)
    grade = calendar_grade.grade_month(rows, profile="GYM")
    assert grade.scores["path_to_join"] == 0, grade.scores
    assert not any("no approved CTA" in k for k in grade.exempt), grade.exempt


def test_the_exemption_fails_OPEN_when_the_pool_cannot_be_read(monkeypatch):
    """A relaxation must never be granted by accident. If we cannot prove the
    pool is empty, the gym is graded exactly as it was before."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")

    def _boom(gym_id, log):
        raise RuntimeError("client_sources unavailable")

    monkeypatch.setattr(grade_fix, "_booking_cta_pool", _boom)
    rows = _book(31)
    grade = calendar_grade.grade_month(rows, profile="GYM")
    assert grade.scores["path_to_join"] == 0, grade.scores
    assert not any("no approved CTA" in k for k in grade.exempt), grade.exempt


def test_the_exemption_needs_the_flag(monkeypatch):
    monkeypatch.delenv("AGENT_CTA_VARIETY", raising=False)
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: [])
    rows = _book(31)
    grade = calendar_grade.grade_month(rows, profile="GYM")
    assert grade.scores["path_to_join"] == 0, grade.scores
    assert not any("no approved CTA" in k for k in grade.exempt), grade.exempt
