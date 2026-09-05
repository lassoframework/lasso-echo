"""The 2026-09-05 Tough Temple double post, and the guard that closes it.

WHAT HAPPENED. A paying client's Instagram received six publishes in 40 seconds, including
a re-publish of a day that had already gone out 19 hours earlier:

    "You showed up even though the treadmill..."   IG 2026-09-05 01:29:33Z
    "You showed up even though the treadmill..."   IG 2026-09-05 20:01:17Z
    "You walk in and it's not what you expected"   IG 20:00:45Z and again 20:00:57Z
    same caption                                   FB 20:00:38Z and again 20:01:10Z

Fleet wide the same signature covered 84 extra publishes across 10 gyms: eng 23, lasso 19,
piercefitness 15, topfuel 7, zanshin 6. Every pair carried a DIFFERENT late_post_id, so
Zernio accepted each as a separate post and they are live on client accounts.

WHY THE EXISTING CLAIM DID NOT STOP IT. mark_publishing is an exactly-once claim on a ROW.
These were DIFFERENT ROWS: same gym, same account, same post_date, same caption, different
time_slot. The planner wrote one caption into two slots and the publisher correctly
published both. A row-id key cannot see that. The key that matters is CONTENT reaching an
ACCOUNT.

The lasso rows prove the window is not a day: the same caption went out 08-14, 08-18 and
09-01.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import calendar_autopublish as cap  # noqa: E402


class _Acct:
    def __init__(self, key="toughtemple52040e_ig", platform="instagram"):
        self.key = key
        self.platform = platform


def _row(caption="You showed up even though the treadmill felt like the last place",
         fmt="feed", **kw):
    r = {"id": "r1", "gym_id": "toughtemple52040e", "account": "instagram",
         "post_date": "2026-09-04", "caption": caption, "format": fmt}
    r.update(kw)
    return r


# ---- the key itself ---------------------------------------------------------------------

def test_the_same_caption_to_the_same_account_is_one_key():
    a = _Acct()
    assert cap._published_content_key(a, _row()) == cap._published_content_key(a, _row())


def test_whitespace_and_case_cannot_slip_a_repeat_past_the_guard():
    a = _Acct()
    k1 = cap._published_content_key(a, _row(caption="You Showed   Up Today"))
    k2 = cap._published_content_key(a, _row(caption="you showed up today"))
    assert k1 == k2 and k1 != ""


def test_cross_posting_to_a_second_platform_is_still_allowed():
    """A gym legitimately puts the same caption on its Instagram AND its Facebook.
    Blocking that would be a regression, so the key is scoped to the ACCOUNT."""
    ig = cap._published_content_key(_Acct("tt_ig", "instagram"), _row())
    fb = cap._published_content_key(_Acct("tt_fb", "facebook"), _row())
    assert ig and fb and ig != fb


def test_a_story_is_exempt():
    """A story has an empty body by design; keying stories on content would collapse
    every story a gym ever posts into one."""
    assert cap._published_content_key(_Acct(), _row(fmt="story")) == ""


def test_an_empty_caption_forms_no_key():
    assert cap._published_content_key(_Acct(), _row(caption="   ")) == ""


def test_an_unformable_key_never_raises():
    class _Bad:
        @property
        def platform(self):
            raise RuntimeError("boom")
    assert cap._published_content_key(_Bad(), _row()) == ""


# ---- the exact production pairs ---------------------------------------------------------

def test_the_tough_temple_pair_collides():
    """Same caption, same account, DIFFERENT time_slot: the pair the row claim could not
    see. Both rows must resolve to one content key."""
    a = _Acct()
    early = _row(time_slot="early_morning", id="a")
    evening = _row(time_slot="evening", id="b")
    assert cap._published_content_key(a, early) == cap._published_content_key(a, evening)


def test_a_different_caption_on_the_same_day_is_not_blocked():
    """A gym running 2x a day with genuinely different copy must still publish both."""
    a = _Acct()
    k1 = cap._published_content_key(a, _row(caption="first post of the day"))
    k2 = cap._published_content_key(a, _row(caption="second post of the day"))
    assert k1 != k2


def test_the_lasso_multi_week_repeat_is_one_key():
    """08-14, 08-18 and 09-01 carried identical copy. Date is deliberately NOT in the key,
    because a repeat weeks later is still a repeat."""
    a = _Acct("lasso_ig", "instagram")
    cap_text = "More leads never fix a broken sales conversation. We get the"
    assert (cap._published_content_key(a, _row(caption=cap_text, post_date="2026-08-14"))
            == cap._published_content_key(a, _row(caption=cap_text, post_date="2026-09-01")))
