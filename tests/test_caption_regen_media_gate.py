"""The CAPTION-ONLY repair lane: why the shipped body-copy repair did nothing.

MEASURED ON PRODUCTION, 2026-09-07. PR #65 shipped `_fix_body_sameness` and
PR #67 stopped the craft pass starving it of budget. Both landed, both deployed,
and all three real books were still exactly where they started:

  gym               body max        pairs      forward grade
  hillcountry     1.0 -> 1.0       8 -> 8         89 (B)
  topfuel         1.0 -> 1.0       5 -> 5         91 (A)
  train7164ae502  1.0 -> 1.0      17 -> 17        89 (B)

Isolating the pass (every earlier pass neutered, the WHOLE 300s budget handed
to the body repair, no competition at all) produced `body_fixed: 0` and five
log lines saying "source material too thin". It was never a budget problem.
Every one of those five `caption_regen` calls came back None, and instrumenting
`_clean_draft_for_day` gave one identical drop reason on all twenty attempts:

    not A+: no media (empty creative url)

hillcountry's media library is fully served, so the builder can still WRITE a
clean caption but has no unused photo to attach -- and `post_quality.post_issues`
failed the whole draft for the photo. The repair passes never wanted the photo:
they rewrite the caption of a day that already carries its own approved image,
which `_default_caption_regen` has said in a comment since the day it was
written ("we only borrow the caption; the row keeps its own photo").

Three defects, all of them assertions this file makes:

  1. THE MEDIA GATE (`require_media`). A caption-only consumer is failed for a
     photo it is not using. Fixed in post_quality -> client_month_run ->
     grade_fix, default TRUE everywhere so no build path changes.
  2. THE HOOK (`_mechanical_repair` in the body pass). With the media gate
     lifted the fresh captions arrive, and 15 of 16 were then thrown away on
     `hook_too_long` alone -- a defect `_fix_craft` has repaired mechanically
     since 2026-08-31 by moving ONE line break and writing no words.
  3. THE MIX (`_cat_headroom`). A fresh caption brings its own pillar, so a body
     repair MOVES a post between categories. Unguarded on topfuel's live book it
     took body max 1.0 -> 0.087 and pairs 4 -> 0 but pushed 'offer' to 26%, and
     the book went 91 (A) -> 87 (B). `_cat_posts` already carries the ruling:
     "A repair must never be able to lower the grade."

And one starvation defect PR #67's craft cutoff could not see: re-measured with
per-pass wall clocks, the pass eating hillcountry's budget is `_fix_overcap`,
not `_fix_craft` (97 of 90 seconds, two regens, both None; craft got 0.0s).
`_OVERCAP_LLM_FAILURE_CAP` is that pass's cutoff.

After the fix, on the same live books through `grade_sweep.run`:

  hillcountry     1.0  -> 0.2308   8 -> 1     89 (B) -> 100 (A)
  topfuel         1.0  -> 0.087    5 -> 0     91 (A) ->  91 (A)
  train7164ae502  1.0  -> 0.5     17 -> 6     89 (B) -> 100 (A)

Every test here is deterministic and offline.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import calendar_grade, client_month_run, post_quality
from agent.jobs import grade_fix

from tests.test_body_repair import (_FakeStore, _PermissiveStore, _hook,
                                    _phrase, _post, _rows)

TODAY = "2026-09-06"


class _Draft:
    """The shape `post_quality` reads: a caption, a creative url, no grounding."""

    def __init__(self, caption, url="https://cdn.example/photo.jpg", category="community"):
        self.caption = caption
        self.creative_public_url = url
        self.category = category
        self.day_key = ""
        self.scheduled_for = ""


def _clean_caption():
    """A caption that clears every A+ CAPTION check, so the ONLY thing that can
    fail a draft carrying it is the media check."""
    return _post(11, 11)


# ---------------------------------------------------------------------------
# 1. post_quality: the media check is the one thing require_media turns off
# ---------------------------------------------------------------------------

def test_a_medialess_draft_is_not_a_plus_by_default():
    """The default is the PRODUCTION default and it is unchanged: a post needs
    a photo. Asserted without passing require_media at all, so a flipped default
    fails here."""
    d = _Draft(_clean_caption(), url="")
    assert post_quality.is_a_plus(d) is False
    assert any("no media" in i for i in post_quality.post_issues(d))


def test_require_media_false_drops_only_the_media_issue():
    d = _Draft(_clean_caption(), url="")
    assert post_quality.is_a_plus(d, require_media=False) is True
    assert post_quality.post_issues(d, require_media=False) == []
    # ... and the draft WITH media is unaffected either way.
    ok = _Draft(_clean_caption())
    assert post_quality.is_a_plus(ok) is True
    assert post_quality.is_a_plus(ok, require_media=False) is True


def test_require_media_false_still_enforces_every_caption_bar():
    """The lifted check is the photo and NOTHING else. A caption-only consumer
    that could smuggle a banned word or an off-avatar hook past the gate would
    be a worse bug than the one this fixes."""
    banned = _Draft("Cheap deal. " + _phrase(0, 40) + ".", url="")
    assert post_quality.post_issues(banned, ("cheap",), require_media=False)
    thin = _Draft("Hi.", url="")
    assert post_quality.post_issues(thin, require_media=False)


# ---------------------------------------------------------------------------
# 2. client_month_run: the day builder threads it, and its default is intact
# ---------------------------------------------------------------------------

def _stub_builder(monkeypatch, draft):
    """Make `build_client_draft` hand back one fixed draft for every day key."""
    from agent import client_content
    monkeypatch.setattr(client_content, "build_client_draft",
                        lambda *a, **k: draft)


def test_the_day_builder_drops_a_medialess_draft_by_default(monkeypatch):
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    _stub_builder(monkeypatch, _Draft(_clean_caption(), url=""))
    draft, drop = client_month_run._clean_draft_for_day(
        object(), "2026-09-10", object(), "/tmp/lib", (), lambda m: None,
        allow_reuse=True)
    assert draft is None
    assert "no media" in (drop or "")


def test_the_day_builder_keeps_a_medialess_draft_for_a_caption_only_caller(monkeypatch):
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    cap = _clean_caption()
    _stub_builder(monkeypatch, _Draft(cap, url=""))
    draft, drop = client_month_run._clean_draft_for_day(
        object(), "2026-09-10", object(), "/tmp/lib", (), lambda m: None,
        allow_reuse=True, require_media=False)
    assert drop is None
    assert draft is not None and draft.caption == cap


# ---------------------------------------------------------------------------
# 3. THE PRODUCTION BUG: the SHIPPED regen closure asks for no photo
#
# This is the assertion the two waves before this one were missing. Both built
# a repair, neither ever ran the repair's own default regen against a book whose
# library is served -- which is every book the repair was built for.
# ---------------------------------------------------------------------------

def test_the_default_regen_returns_a_caption_when_no_unused_media_is_left(monkeypatch):
    """`_default_caption_regen` is what production uses (the caller passes no
    caption_regen). Its closure must survive a builder with no photo to give."""
    seen = {}

    def fake_clean_draft(account, day_key, voice, library_path, banned, log,
                         **kw):
        seen.update(kw)
        if kw.get("require_media", True):
            return None, "not A+: no media (empty creative url)"
        return _Draft(_clean_caption(), url="", category="education"), None

    monkeypatch.setattr(client_month_run, "_clean_draft_for_day",
                        fake_clean_draft)
    monkeypatch.setattr(grade_fix, "_default_db", lambda: None)

    regen = _build_default_regen(monkeypatch)
    out = regen({"post_date": "2026-09-10"}, set(), "")
    assert seen.get("require_media") is False, (
        "the caption-only lane asked the builder for a photo it never uses")
    assert out is not None, (
        "the shipped regen returned None on a book with no unused media: this "
        "is the live hillcountry defect")
    assert out[0] == _clean_caption() and out[1] == "education"


def _build_default_regen(monkeypatch):
    """`_default_caption_regen` with its context resolution stubbed, so the test
    exercises the REAL closure body (the part that calls the day builder)."""
    from agent import client_media_sync, voice as voice_mod

    class _Acct:
        def voice_doc_path(self):
            return "/tmp/voice.md"

    monkeypatch.setattr(client_media_sync, "_account_for_base",
                        lambda g: _Acct())
    monkeypatch.setattr(client_media_sync, "_library_dir", lambda g: "/tmp/lib")
    monkeypatch.setattr(client_media_sync, "_resolve_client_voice_path",
                        lambda g, p: p)
    monkeypatch.setattr(client_media_sync, "_banned_words_for", lambda g: ())
    monkeypatch.setattr(voice_mod, "load_voice", lambda p: object())
    regen = grade_fix._default_caption_regen("hillcountry", "GYM",
                                             lambda m: None)
    assert regen is not None
    return regen


# ---------------------------------------------------------------------------
# 4. The body pass re-lineates an over-long hook instead of discarding it
# ---------------------------------------------------------------------------

def _long_hook_caption():
    """A caption whose ONLY craft defect is a first line over the 125 char hook
    band, breakable at its first sentence boundary -- the exact shape 15 of
    hillcountry's 16 fresh drafts arrived in."""
    cap = f"{_hook(300)} {_phrase(2000, 24)}.\n\n{_phrase(3000, 20)}."
    first = cap.splitlines()[0]
    assert len(first) > grade_fix._HOOK_MAX
    assert not grade_fix._clears_craft(cap, allow_no_ask=True)
    assert grade_fix._clears_craft(grade_fix._mechanical_repair(cap, None),
                                   allow_no_ask=True)
    return cap


def _hook_twins(n=2, gym_id="hill"):
    """`n` posts sharing one hook verbatim: body similarity 1.0."""
    return _rows([(f"2026-09-{7 + i:02d}", _post(0, i), "pending")
                  for i in range(n)], gym_id=gym_id)


def test_a_fresh_caption_with_a_long_hook_is_relineated_and_lands(monkeypatch):
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _hook_twins(2)
    fresh = _long_hook_caption()
    pairs, fixed, unrepairable = grade_fix._fix_body_sameness(
        "hill", rows, _PermissiveStore(rows),
        lambda row, avoid, cat="": (fresh, "community"), set(),
        lambda m: None)
    assert (pairs, fixed, unrepairable) == (1, 1, 0), (
        "a caption whose only defect is an over-long hook was discarded")
    landed = rows[1]["caption"]
    assert landed != fresh, "the hook was never re-lineated"
    assert grade_fix._clears_craft(landed, allow_no_ask=True)
    # ZERO FABRICATION: only a line break moved, the words are identical.
    assert landed.split() == fresh.split()


def test_a_fresh_caption_mechanics_cannot_save_is_still_refused(monkeypatch):
    """The re-lineation is not a way past the craft bar: a caption that is thin
    (nothing a line break can fix) is still left in place."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _hook_twins(2)
    before = rows[1]["caption"]
    pairs, fixed, unrepairable = grade_fix._fix_body_sameness(
        "hill", rows, _PermissiveStore(rows),
        lambda row, avoid, cat="": ("Short.", "community"), set(),
        lambda m: None)
    assert (pairs, fixed, unrepairable) == (1, 0, 1)
    assert rows[1]["caption"] == before


# ---------------------------------------------------------------------------
# 5. The body repair never trades a repeated body for an over-cap category
# ---------------------------------------------------------------------------

def _mix_book(gym_id="topfuel"):
    """A month-sized book (>= _MIX_CAP_MIN_POSTS) with 'offer' already sitting
    exactly ON the 25% cap, plus two posts that share a hook verbatim."""
    n = calendar_grade._MIX_CAP_MIN_POSTS          # 12 posts
    at_cap = int(0.25 * n)                         # 3 offer posts = 25%
    specs = []
    for i in range(n - 2):
        specs.append((f"2026-09-{10 + i:02d}", _post(100 + i, 500 + i), "pending"))
    specs.append(("2026-10-01", _post(0, 800), "pending"))
    specs.append(("2026-10-02", _post(0, 801), "pending"))
    rows = _rows(specs, gym_id=gym_id)
    for i, r in enumerate(rows):
        r["pillar"] = r["category"] = "offer" if i < at_cap else f"pillar{i}"
    return rows


def test_a_repair_that_would_break_the_25_percent_cap_is_not_made(monkeypatch):
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _mix_book()
    before = rows[-1]["caption"]
    fresh = _post(700, 700)
    assert grade_fix._clears_craft(fresh, allow_no_ask=True)
    pairs, fixed, unrepairable = grade_fix._fix_body_sameness(
        "topfuel", rows, _PermissiveStore(rows),
        lambda row, avoid, cat="": (fresh, "offer"), set(), lambda m: None)
    assert (pairs, fixed, unrepairable) == (1, 0, 1), (
        "the repair pushed a category over the grader's 25% cap")
    assert rows[-1]["caption"] == before


def test_the_same_repair_lands_when_the_target_pillar_has_room(monkeypatch):
    """The guard is a headroom test, not a refusal to move pillars at all."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _mix_book()
    fresh = _post(700, 700)
    pairs, fixed, unrepairable = grade_fix._fix_body_sameness(
        "topfuel", rows, _PermissiveStore(rows),
        lambda row, avoid, cat="": (fresh, "roomy"), set(), lambda m: None)
    assert (pairs, fixed, unrepairable) == (1, 1, 0)
    assert rows[-1]["caption"] == fresh
    assert rows[-1]["pillar"] == "roomy"


def test_a_book_too_small_for_the_cap_is_not_guarded(monkeypatch):
    """The grader exempts books below `_MIX_CAP_MIN_POSTS` from the 25% cap, so
    guarding them here would refuse real repairs to dodge a defect that is never
    measured (on a 4 post book the cap allows ONE post per pillar)."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _hook_twins(2)
    assert len(rows) < calendar_grade._MIX_CAP_MIN_POSTS
    fresh = _post(700, 700)
    pairs, fixed, unrepairable = grade_fix._fix_body_sameness(
        "hill", rows, _PermissiveStore(rows),
        lambda row, avoid, cat="": (fresh, "community"), set(), lambda m: None)
    assert (pairs, fixed, unrepairable) == (1, 1, 0)


# ---------------------------------------------------------------------------
# 6. The over-cap pass concedes too -- it is the pass that actually starved
#    hillcountry, and PR #67's craft cutoff cannot see it.
# ---------------------------------------------------------------------------

def test_the_shipped_overcap_cutoff_default_is_two():
    """Asserted on the SHIPPED constant, not on an override. Every other test in
    this section drives the real default through the pass."""
    assert grade_fix._OVERCAP_LLM_FAILURE_CAP == 2


def _overcap_book(n=10, gym_id="hill"):
    """`n` posts all in one pillar, so 'offer' is far over the 25% cap."""
    rows = _rows([(f"2026-09-{7 + i:02d}", _post(400 + i, 600 + i), "pending")
                  for i in range(n)], gym_id=gym_id)
    for r in rows:
        r["pillar"] = r["category"] = "offer"
    return rows


_OVERCAP_DEFECT = [("content_mix", "offer", "offer is 100% of posts (over 25%)")]


def test_the_overcap_pass_stops_after_two_fruitless_regens(monkeypatch):
    """hillcountry, live: TWO over-cap regens returned None and spent 97 of the
    90 second budget, and every pass behind them got nothing."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _overcap_book(10)
    calls = []

    def regen(row, avoid, avoid_category=""):
        calls.append(str((row or {}).get("post_date") or ""))
        return None

    fixed, skipped = grade_fix._fix_overcap(
        "hill", rows, _FakeStore(rows), _OVERCAP_DEFECT, regen, set(),
        lambda m: None)
    assert fixed == 0
    assert len(calls) == grade_fix._OVERCAP_LLM_FAILURE_CAP, (
        f"the over-cap pass made {len(calls)} LLM calls on a book that answered "
        "the same way every time")


def test_a_success_resets_the_overcap_cutoff(monkeypatch):
    """What the cutoff detects is "this book has nothing", not "enough work
    done": a pass that is still landing moves must never be cut off."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _overcap_book(10)
    calls = []
    # fail, fail would trip a naive counter; the success between them must not.
    script = [None, "win", None, "win", None, "win", None, None]

    def regen(row, avoid, avoid_category=""):
        i = len(calls)
        calls.append(str((row or {}).get("post_date") or ""))
        step = script[i] if i < len(script) else None
        return (_post(9000 + i, 9500 + i), f"fresh{i}") if step else None

    fixed, _skipped = grade_fix._fix_overcap(
        "hill", rows, _FakeStore(rows), _OVERCAP_DEFECT, regen, set(),
        lambda m: None)
    assert fixed >= 3, "a success did not reset the consecutive-failure run"
    assert len(calls) > grade_fix._OVERCAP_LLM_FAILURE_CAP


def test_the_overcap_cutoff_is_inert_with_the_flag_off(monkeypatch):
    """Flag off = the pre-2026-09-07 posture, byte for byte."""
    monkeypatch.delenv("AGENT_CTA_VARIETY", raising=False)
    rows = _overcap_book(10)
    calls = []

    def regen(row, avoid, avoid_category=""):
        calls.append(1)
        return None

    grade_fix._fix_overcap("hill", rows, _FakeStore(rows), _OVERCAP_DEFECT,
                           regen, set(), lambda m: None)
    assert len(calls) > grade_fix._OVERCAP_LLM_FAILURE_CAP


def test_the_overcap_cutoff_yields_budget_to_the_body_pass(monkeypatch):
    """THE POINT OF THE CUTOFF, end to end: with the over-cap pass conceding,
    the body repair at the back of the queue still gets its turn and lands."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: [])
    rows = _overcap_book(8) + _hook_twins(2, gym_id="hill")
    for i, r in enumerate(rows):
        r["id"] = f"row_{i}"
    body_days = []

    def regen(row, avoid, avoid_category=""):
        day = str((row or {}).get("post_date") or "")
        if avoid_category:                       # the over-cap pass: never works
            return None
        body_days.append(day)
        return _post(9000 + len(body_days), 9500 + len(body_days)), "community"

    out = grade_fix.remediate_forward_book(
        "hill", rows, _PermissiveStore(rows), profile="GYM",
        defects=_OVERCAP_DEFECT, today_iso=TODAY, caption_regen=regen,
        gap_filler=lambda *a, **k: "none", logger=lambda m: None)
    assert out["repillared"] == 0
    assert out["body_fixed"] == 1, out


if __name__ == "__main__":                       # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
