"""
IG feed hashtag fold (agent/ig_feed_hashtags.py + the fold seam in
agent/real_calendar_mirror._real_row), all offline.

The stored IG feed caption must end with exactly ONE final line of 3-5 hashtags
drawn ONLY from the gym's approved VoiceDoc; the portal preview and the Zernio
wire share that stored base caption when optional mentions are disabled. Facebook rows, story rows, and
terminal-status rows are never touched; re-runs are byte-identical; a tag
already in the caption is never duplicated; a doc with fewer than 3 usable tags
contributes only what exists.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import ig_feed_hashtags as igfh
from agent import backfill_ig_hashtags as bih
from agent import client_month_run as cmr
from agent import real_calendar_mirror as rcm
from agent import real_month_planner as rmp
from agent.drafter import Draft, DraftStatus
from agent.voice import load_voice


VOICE_RAW = """# Brand Bible

### Hashtags
#GymOne #GymTwo #GymThree #GymFour #GymFive #GymSix #GymSeven

### Colors
Primary #121E3C accent #FF0000
"""


def _voice(raw=VOICE_RAW, tmp_path=None, name="voice.md"):
    path = tmp_path / name
    path.write_text(raw, encoding="utf-8")
    return load_voice(str(path))


# ---- ensure_feed_tag_line (the pure rule) ----------------------------------

def test_appends_one_final_line_of_up_to_5_approved_tags(tmp_path):
    voice = _voice(tmp_path=tmp_path)
    out = igfh.ensure_feed_tag_line("Real caption body.", voice)
    body, _, tail = out.rpartition("\n\n")
    assert body == "Real caption body.", "the body is preserved byte-for-byte"
    tags = tail.split()
    assert 3 <= len(tags) <= 5
    assert all(t.startswith("#") for t in tags)
    assert set(tags) <= set(voice.hashtags), "approved tags only, never invented"
    # hex colors from the visual-identity section are never tags
    assert "#121E3C" not in tags and "#FF0000" not in tags


def test_rerun_is_byte_identical(tmp_path):
    voice = _voice(tmp_path=tmp_path)
    once = igfh.ensure_feed_tag_line("Real caption body.", voice)
    twice = igfh.ensure_feed_tag_line(once, voice)
    assert twice == once


def test_compliant_existing_line_is_left_untouched(tmp_path):
    voice = _voice(tmp_path=tmp_path)
    caption = "Body.\n\n#GymTwo #GymFive #GymOne"
    assert igfh.ensure_feed_tag_line(caption, voice) == caption


def test_never_duplicates_a_tag_already_in_the_body(tmp_path):
    voice = _voice(tmp_path=tmp_path)
    out = igfh.ensure_feed_tag_line("We love #GymOne around here.", voice)
    assert out.lower().count("#gymone") == 1
    assert "#GymOne" not in out.rpartition("\n\n")[2]


def test_manual_unapproved_trailing_tag_line_is_preserved(tmp_path):
    voice = _voice(tmp_path=tmp_path)
    caption = "Body.\n\n#MadeUp #AlsoMadeUp"
    assert igfh.ensure_feed_tag_line(caption, voice) == caption
    assert igfh.final_tag_line_kind(caption, voice.hashtags) == "manual"


def test_existing_short_approved_tag_line_is_owned_copy_and_preserved(tmp_path):
    voice = _voice(tmp_path=tmp_path)
    caption = "Body.\n\n#GymOne"
    assert igfh.ensure_feed_tag_line(caption, voice) == caption
    assert igfh.final_tag_line_kind(caption, voice.hashtags) == "compliant"


def test_fewer_than_three_approved_tags_uses_only_what_exists(tmp_path):
    voice = _voice(raw="# Doc\n\nOnly #SoloOne and #SoloTwo.\n", tmp_path=tmp_path)
    out = igfh.ensure_feed_tag_line("Body.", voice)
    assert out.startswith("Body.\n\n")
    assert set(out.rpartition("\n\n")[2].split()) == {"#SoloOne", "#SoloTwo"}, \
        "both approved tags, nothing invented, never padded to 3"
    # ...and a re-run over that short line is stable
    assert igfh.ensure_feed_tag_line(out, voice) == out


def test_no_approved_tags_or_no_voice_leaves_caption_untouched(tmp_path):
    empty_voice = _voice(raw="# Doc\n\nNo tags here.\n", tmp_path=tmp_path)
    assert igfh.ensure_feed_tag_line("Body.", empty_voice) == "Body."
    assert igfh.ensure_feed_tag_line("Body.", None) == "Body."


def test_empty_caption_stays_empty(tmp_path):
    voice = _voice(tmp_path=tmp_path)
    assert igfh.ensure_feed_tag_line("", voice) == ""
    assert igfh.ensure_feed_tag_line("   \n", voice) == "   \n"


def test_caption_that_is_only_tags_is_left_alone(tmp_path):
    voice = _voice(tmp_path=tmp_path)
    caption = "#GymOne #GymTwo"
    assert igfh.ensure_feed_tag_line(caption, voice) == caption


def test_selection_is_deterministic_per_creative_stem(tmp_path):
    voice = _voice(tmp_path=tmp_path)
    a1 = igfh.ensure_feed_tag_line("Body.", voice, SimpleNamespace(path="a/b/card1.png"))
    a2 = igfh.ensure_feed_tag_line("Body.", voice, SimpleNamespace(path="a/b/card1.png"))
    assert a1 == a2


def test_dedupes_and_replenishes_without_repeating_body_tags(tmp_path):
    voice = _voice(raw="# Doc\n#One #Two #Three #Four #Five #Six\n", tmp_path=tmp_path)
    out = igfh.ensure_feed_tag_line(
        "Body already has #One.", voice,
        selected_tags=["#One", "#One", "#Two"])
    tail = out.rpartition("\n\n")[2].split()
    assert tail == ["#Two", "#Three", "#Four", "#Five", "#Six"]
    assert out.lower().count("#one") == 1


def test_compliant_final_line_is_byte_identical_including_trailing_space(tmp_path):
    voice = _voice(tmp_path=tmp_path)
    caption = "Body.\n\n#GymOne #GymTwo #GymThree  "
    assert igfh.ensure_feed_tag_line(caption, voice) == caption


def test_adding_tags_preserves_existing_body_bytes(tmp_path):
    voice = _voice(tmp_path=tmp_path)
    body = "Body with owned spacing.  \n"
    out = igfh.ensure_feed_tag_line(body, voice)
    assert out.startswith(body)
    assert out[len(body):].startswith("\n#")


# ---- the mirror seam (_real_row / collect_real_drafts) ----------------------

def _draft(draft_id, account_key="northside_ig", platform="instagram",
           day_key="2026-09-19", caption="real body", status=DraftStatus.PENDING,
           is_story=False, draft_type="feed", hashtags=None):
    return Draft(draft_id=draft_id, account_key=account_key, platform=platform,
                 caption=caption, hashtags=(hashtags if hashtags is not None else
                                             ["#GymOne", "#GymTwo", "#GymThree"]), creative_path="x.png",
                 creative_public_url="https://cdn/x.jpg", scheduled_for="",
                 status=status, is_story=is_story, day_key=day_key,
                 draft_type=draft_type, category="proof")


def test_real_row_folds_draft_selected_tags_for_ig_feed():
    row = rcm._real_row("northside_ig", _draft("f1"))
    tail = row["caption"].rpartition("\n\n")[2]
    assert 3 <= len(tail.split()) <= 5
    assert row["caption"].startswith("real body\n\n")


def test_real_row_never_touches_facebook_story_or_terminal_rows():
    fb = rcm._real_row("northside_ig", _draft("fb1", platform="facebook_page"))
    assert fb["caption"] == "real body", "Facebook copy stays unchanged"
    story = rcm._real_row("northside_ig",
                          _draft("s1", is_story=True, draft_type="story", caption=""))
    assert story["caption"] == "", "stories stay empty-body"
    for status in (DraftStatus.SKIPPED, DraftStatus.SUPERSEDED, DraftStatus.EXPIRED):
        row = rcm._real_row("northside_ig", _draft("d1", status=status))
        assert row["caption"] == "real body", f"{status} (denied) is never edited"
    raw_terminal = _draft("d2")
    object.__setattr__(raw_terminal, "status", "published")
    row = rcm._real_row("northside_ig", raw_terminal)
    assert row["caption"] == "real body", "a raw terminal status is never edited"


def test_real_row_without_draft_tags_leaves_caption_untouched():
    row = rcm._real_row("northside_ig", _draft("f1", hashtags=[]))
    assert row["caption"] == "real body"


def test_collect_real_drafts_rerun_is_idempotent():

    class _Store:
        def list_for_account(self, key):
            return [_draft("f1", account_key=key)]

    once = rcm.collect_real_drafts("northside_ig", _Store())[0]["caption"]
    # the second pass sees the already-folded caption (as a stored draft would
    # carry it) and must not add a second tag line
    class _Store2:
        def list_for_account(self, key):
            return [_draft("f1", account_key=key, caption=once)]

    twice = rcm.collect_real_drafts("northside_ig", _Store2())[0]["caption"]
    assert twice == once


def test_client_crosspost_keeps_facebook_caption_clean():
    rows = cmr._to_rows("northside", [_draft("f1")])
    ig = next(row for row in rows if row["account"] == "instagram")
    fb = next(row for row in rows if row["account"] == "facebook")
    assert "#GymOne" in ig["caption"]
    assert fb["caption"] == "real body"


def test_lasso_editorial_caption_gets_tags_after_editorial_replacement(monkeypatch):
    from agent import config, lasso_editorial

    monkeypatch.setattr(config, "lasso_editorial_calendar_enabled", lambda: True)
    monkeypatch.setattr(lasso_editorial, "editorial_caption", lambda draft: "Editorial body.")
    rows = rmp.to_calendar_rows([_draft("f1", account_key="lasso_ig")], "lasso")
    ig = next(row for row in rows if row["account"] == "instagram")
    fb = next(row for row in rows if row["account"] == "facebook")
    assert ig["caption"].startswith("Editorial body.\n\n#GymOne")
    assert fb["caption"] == "Editorial body."


def test_backfill_skips_manual_and_requires_known_mutable_status(tmp_path):
    voice = _voice(tmp_path=tmp_path)
    manual = {
        "id": "manual", "account": "instagram", "format": "feed",
        "status": "pending", "variant_status": "active", "post_date": "2026-09-20",
        "caption": "Body.\n\n#ManualTag",
    }
    approved = dict(manual, id="approved", status="approved", caption="Body.")
    denied = dict(manual, id="denied", status="denied", caption="Body.")
    plan = bih.plan_gym("northside", [manual, approved, denied], voice, "2026-09-19")
    assert [change["row_id"] for change in plan["changes"]] == ["approved"]
    assert plan["skipped"]["manual_tag_line"] == 1
    assert plan["skipped"]["status_denied"] == 1


def test_backfill_uses_atomic_patch_and_reapproval_for_approved(tmp_path):
    voice = _voice(tmp_path=tmp_path)

    class _Store:
        def __init__(self):
            self.calls = []

        def list_month(self, base, month):
            return [{
                "id": "row-1", "account": "instagram", "format": "feed",
                "status": "approved", "variant_status": "active",
                "post_date": "2026-09-20", "caption": "Body.",
            }]

        def patch_caption_for_hashtag_backfill(self, base, row_id, caption, *, expected_status):
            self.calls.append((base, row_id, caption, expected_status))
            return {"id": row_id, "status": "pending"}

    store = _Store()
    summary = bih.run(["northside"], store=store, voice_loader=lambda _: voice,
                      today_iso="2026-09-19", months=1, apply=True, printer=lambda _: None)
    assert summary["written"] == 1
    assert store.calls[0][0:2] == ("northside", "row-1")
    assert store.calls[0][3] == "approved"


def test_store_backfill_patch_has_atomic_status_and_publish_guards(monkeypatch):
    from agent import portal_calendar_store as pcs

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return [{"id": "row-1", "gym_id": "northside", "status": "pending"}]

    class _Http:
        def __init__(self):
            self.calls = []

        def patch(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return _Response()

    http = _Http()
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    out = pcs.SupabaseCalendarStore().patch_caption_for_hashtag_backfill(
        "northside", "row-1", "Body.\n\n#GymOne", expected_status="approved")
    assert out["status"] == "pending"
    _args, kwargs = http.calls[0]
    assert kwargs["params"] == {
        "id": "eq.row-1", "gym_id": "eq.northside", "status": "eq.approved",
        "published_at": "is.null", "late_post_id": "is.null", "variant_status": "eq.active",
    }
    assert kwargs["json"] == {"caption": "Body.\n\n#GymOne", "status": "pending"}
    assert pcs.SupabaseCalendarStore().patch_caption_for_hashtag_backfill(
        "northside", "row-1", "x", expected_status="denied") is None


def test_portal_calendar_and_publish_draft_use_the_identical_stored_caption():
    from agent import calendar_autopublish as cap
    from agent import portal_social

    row = {
        "id": "ig-1", "gym_id": "northside", "account": "instagram",
        "format": "feed", "post_date": "2026-09-20", "status": "pending",
        "caption": "Body.\n\n#GymOne #GymTwo #GymThree", "image_url": "https://cdn/x.jpg",
    }
    assert portal_social._content_calendar_post(row)["caption"] == row["caption"]
    assert cap._draft_for(row).caption == row["caption"]


def test_portal_preview_and_zernio_base_body_match_with_mentions_disabled(monkeypatch):
    from agent import calendar_autopublish as cap
    from agent import config, portal_social, publish_billing_gate, zernio_publisher
    from agent.accounts import Account, Platform

    row = {
        "id": "ig-1", "gym_id": "northside", "account": "instagram",
        "format": "feed", "post_date": "2026-09-20", "status": "pending",
        "caption": "Body.\n\n#GymOne #GymTwo #GymThree", "image_url": "https://cdn/x.jpg",
    }
    monkeypatch.setattr(config, "publish_enabled", lambda: True)
    monkeypatch.setattr(config, "zernio_publish_enabled", lambda: True)
    monkeypatch.setattr(config, "mentions_enabled", lambda: False)
    monkeypatch.setattr(publish_billing_gate, "publishing_blocked", lambda _: False)

    class _Client:
        def list_accounts(self, profile):
            return {"accounts": [{"_id": "ig-1", "platform": "instagram"}]}

        def create_post(self, account_id, body, **kwargs):
            self.body = body
            return {"_id": "z-1"}

    client = _Client()
    draft = cap._draft_for(row)
    draft.account_key = "northside_ig"
    draft.platform = Platform.INSTAGRAM
    zernio_publisher.publish(
        draft,
        Account(key="northside_ig", display_name="Northside IG",
                platform=Platform.INSTAGRAM, token_env="T", target_id_env="G"),
        client=client, profile_resolver=lambda _: "profile")
    assert portal_social._content_calendar_post(row)["caption"] == client.body == row["caption"]
