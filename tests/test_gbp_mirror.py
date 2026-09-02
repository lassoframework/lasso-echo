"""
gbp_mirror tests (agent/gbp_mirror.py), fully offline — no Slack, no Zernio, no R2, no
LLM, no store.

Blake, 2026-09-02: "yes use GBP planner tha anytime you post to ig, fb or whatever goes
to google as well." Google Business content used to come ONLY from the separate monthly
planner, which drew from its own rotation namespace and reached 6 of 10 connected gyms.
This mirrors every FEED post at build time, the same way the Facebook leg does.

Covers the six rules the module docstring commits to: flag OFF is byte-for-byte inert,
stories never mirror, the Facebook duplicate never mirrors again (or Google gets each
post twice), infographics never mirror, a non-'connected' gym gets nothing, a caption
that cannot clear the A+ gate is SKIPPED rather than degraded, rows are always 'pending',
and no gym state failure ever raises into the month build.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, gbp_mirror as gm  # noqa: E402


def _draft(caption="Members showed up at 5am and put in the work today.",
           platform="instagram", day_key="2026-09-10", is_story=False,
           category="community", creative_path="/tmp/x.jpg"):
    return types.SimpleNamespace(
        caption=caption, platform=platform, day_key=day_key, is_story=is_story,
        draft_type="story" if is_story else "feed", category=category,
        creative_path=creative_path, creative_public_url="https://cdn.test/x.jpg",
        scheduled_for=f"{day_key}T10:00:00Z")


def _ctx():
    return {"city": "Cape Coral", "voice": object(), "cta_url": "https://gym.test/join",
            "account_gen_key": "eng_ig"}


def _armed(monkeypatch):
    monkeypatch.setenv("AGENT_GBP_MIRROR", "true")
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)


def _rows(monkeypatch, drafts, **kw):
    """rows_for with the two impure legs injected: a crop that always succeeds and a
    caption generator that echoes the grounding fact."""
    kw.setdefault("ctx", _ctx())
    kw.setdefault("image_fn", lambda d, day: f"https://cdn.test/gbp/{day}.jpg")
    kw.setdefault("caption_fn", lambda fact: f"{fact} Visit us in Cape Coral.")
    kw.setdefault("logger", lambda m: None)
    monkeypatch.setattr("agent.gbp.caption_issues", lambda cap, city=None: [])
    return gm.rows_for("eng", drafts, **kw)


# ---- flag gate -------------------------------------------------------------------------

def test_flag_defaults_off():
    assert config.gbp_mirror_enabled() is False


def test_flag_off_is_byte_for_byte_inert(monkeypatch):
    monkeypatch.delenv("AGENT_GBP_MIRROR", raising=False)
    monkeypatch.delenv("AGENT_GBP_MIRROR_GYMS", raising=False)
    # no ctx, no image_fn, no caption_fn: if anything ran it would blow up or do I/O.
    assert gm.rows_for("eng", [_draft()], logger=lambda m: None) == []


# ---- per-gym rollout rung (same shape as vision_gyms / story_studio_render_gyms) ------

def test_the_pilot_allowlist_arms_only_the_named_gym(monkeypatch):
    monkeypatch.delenv("AGENT_GBP_MIRROR", raising=False)
    monkeypatch.setenv("AGENT_GBP_MIRROR_GYMS", "eng")
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    monkeypatch.setattr("agent.gbp.caption_issues", lambda cap, city=None: [])
    kw = dict(ctx=_ctx(), image_fn=lambda d, day: "https://cdn.test/x.jpg",
              caption_fn=lambda fact: "real copy", logger=lambda m: None)
    assert len(gm.rows_for("eng", [_draft()], **kw)) == 1
    # a gym NOT on the list stays completely inert, same build, same call
    assert gm.rows_for("piercefitness", [_draft()], **kw) == []


def test_the_global_flag_arms_every_gym_and_ignores_the_allowlist(monkeypatch):
    monkeypatch.setenv("AGENT_GBP_MIRROR", "true")
    monkeypatch.setenv("AGENT_GBP_MIRROR_GYMS", "someothergym")
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    monkeypatch.setattr("agent.gbp.caption_issues", lambda cap, city=None: [])
    rows = gm.rows_for("eng", [_draft()], ctx=_ctx(),
                       image_fn=lambda d, day: "https://cdn.test/x.jpg",
                       caption_fn=lambda fact: "real copy", logger=lambda m: None)
    assert len(rows) == 1


def test_active_for_is_case_and_blank_safe():
    assert config.gbp_mirror_active_for("") is False
    assert config.gbp_mirror_active_for(None) is False


# ---- per-build cost: the caches that make a rebuild nearly free -----------------------
# The mirror runs one crop AND one caption generation per FEED POST per build (~268
# fleet-wide), and a month rebuild re-runs every one of them. Three separate caches make
# a rebuild cheap; each is pinned here because a silent cache miss is invisible except on
# the bill.

def test_a_remote_photo_gets_a_deterministic_name_not_a_random_temp(monkeypatch):
    # THE cost leak this fixes: media_host._build_key is
    # echo/<tenant>/<sha1-of-bytes>/<basename>, so a random temp basename changed the R2
    # key every build, defeating content dedupe and writing a NEW object for identical
    # pixels on every rebuild. A url-derived name makes the key stable.
    a = gm.stable_local_name("https://cdn.test/eng/photo-one.jpg", ".jpg")
    b = gm.stable_local_name("https://cdn.test/eng/photo-one.jpg", ".jpg")
    c = gm.stable_local_name("https://cdn.test/eng/photo-two.jpg", ".jpg")
    assert a == b, "same photo must localize to the same filename across builds"
    assert a != c, "different photos must not collide"
    assert a.endswith(".jpg") and "tmp" not in a


def test_an_already_localized_photo_is_not_downloaded_again(monkeypatch, tmp_path):
    monkeypatch.setattr(gm, "_localized_dir", lambda: str(tmp_path))
    url = "https://cdn.test/eng/already-here.jpg"
    # pre-seed the cache exactly as a previous build would have left it
    (tmp_path / gm.stable_local_name(url, ".jpg")).write_bytes(b"realphotobytes")
    calls = []
    monkeypatch.setattr("agent.media_host.download_bytes",
                        lambda u: calls.append(u) or b"x")
    d = _draft(creative_path="")
    d.creative_public_url = url
    path, cleanup = gm._local_still(d, None, lambda m: None)
    assert path and open(path, "rb").read() == b"realphotobytes"
    assert calls == [], "a cached photo must never be re-fetched"
    assert cleanup is None, "a cached source is kept, never deleted as a temp"


def test_a_gate_clearing_caption_is_cached_and_reused_without_a_second_model_call(
        monkeypatch, tmp_path):
    monkeypatch.setattr(gm, "_caption_cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr("agent.gbp.caption_issues", lambda cap, city=None: [])
    calls = []

    def _gen(fact, voice, city):
        calls.append(fact)
        return "Get stronger in Cape Coral. Real coaching, real results."

    monkeypatch.setattr("agent.gbp_planner.generate_gbp_caption", _gen)
    d, ctx = _draft(), _ctx()
    first = gm.gbp_caption(d, ctx, base_key="eng")
    second = gm.gbp_caption(d, ctx, base_key="eng")
    assert first and first == second
    assert len(calls) == 1, "the rebuild must reuse the cached caption, not regenerate"


def test_a_rejected_caption_is_never_cached_so_a_later_build_retries(
        monkeypatch, tmp_path):
    monkeypatch.setattr(gm, "_caption_cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr("agent.gbp.caption_issues", lambda cap, city=None: ["too long"])
    monkeypatch.setattr("agent.gbp_planner.generate_gbp_caption",
                        lambda f, v, c: "a caption the gate rejects")
    assert gm.gbp_caption(_draft(), _ctx(), base_key="eng") is None
    assert list(tmp_path.iterdir()) == [], "a skip must not be cached as an answer"


def test_editing_the_voice_doc_invalidates_its_cached_captions():
    ctx_a, ctx_b = _ctx(), _ctx()
    ctx_a["voice"] = types.SimpleNamespace(raw="voice version one")
    ctx_b["voice"] = types.SimpleNamespace(raw="voice version TWO, rewritten")
    k1 = gm.caption_cache_key("eng", "same fact", "Cape Coral", ctx_a["voice"])
    k2 = gm.caption_cache_key("eng", "same fact", "Cape Coral", ctx_b["voice"])
    assert k1 != k2, "a rewritten bible must not serve copy from the old one"


def test_the_cache_key_separates_gyms_and_cities():
    v = types.SimpleNamespace(raw="one voice")
    base = gm.caption_cache_key("eng", "fact", "Cape Coral", v)
    assert gm.caption_cache_key("topfuel", "fact", "Cape Coral", v) != base
    assert gm.caption_cache_key("eng", "fact", "Valparaiso", v) != base
    assert gm.caption_cache_key("eng", "other fact", "Cape Coral", v) != base


def test_an_injected_caption_fn_is_never_cached(monkeypatch, tmp_path):
    # tests and callers that inject a generator must not pollute the real cache
    monkeypatch.setattr(gm, "_caption_cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr("agent.gbp.caption_issues", lambda cap, city=None: [])
    gm.gbp_caption(_draft(), _ctx(), caption_fn=lambda f: "injected copy",
                   base_key="eng")
    assert list(tmp_path.iterdir()) == []


# ---- what mirrors, and what must never ------------------------------------------------

def test_a_feed_post_mirrors_to_a_googlebusiness_row(monkeypatch):
    _armed(monkeypatch)
    rows = _rows(monkeypatch, [_draft()])
    assert len(rows) == 1
    r = rows[0]
    assert r["account"] == "googlebusiness"
    assert r["gym_id"] == "eng"
    assert r["post_date"] == "2026-09-10"
    assert r["gbp_topic_type"] == "STANDARD"
    assert r["image_url"].startswith("https://cdn.test/gbp/")
    assert r["caption"]


def test_rows_are_always_pending_never_coach_review_never_approved(monkeypatch):
    _armed(monkeypatch)
    rows = _rows(monkeypatch, [_draft()])
    assert rows[0]["status"] == "pending"


def test_stories_never_mirror(monkeypatch):
    _armed(monkeypatch)
    assert _rows(monkeypatch, [_draft(is_story=True)]) == []


def test_the_facebook_duplicate_never_mirrors_again(monkeypatch):
    # THE double-post trap: the build hands us BOTH legs of a cross-posted feed. Only
    # the Instagram original may mirror, or the gym's Google listing gets every post
    # twice.
    _armed(monkeypatch)
    ig = _draft(platform="instagram")
    fb = _draft(platform="facebook")
    rows = _rows(monkeypatch, [ig, fb])
    assert len(rows) == 1


def test_infographics_never_mirror(monkeypatch):
    # a house-rendered card carries its own on-image copy laid out for a square feed;
    # a 4:3 cover-crop would cut its own words off.
    _armed(monkeypatch)
    monkeypatch.setattr(gm, "is_infographic", lambda d: True)
    assert _rows(monkeypatch, [_draft()]) == []


def test_a_draft_with_no_post_date_is_dropped(monkeypatch):
    _armed(monkeypatch)
    d = _draft(day_key="")
    d.scheduled_for = ""
    assert _rows(monkeypatch, [d]) == []


# ---- the A+ gate: skip, never degrade --------------------------------------------------

def test_a_caption_that_fails_the_a_plus_gate_is_skipped_not_shipped(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr("agent.gbp.caption_issues",
                        lambda cap, city=None: ["hook too long"])
    rows = gm.rows_for("eng", [_draft()], ctx=_ctx(),
                       image_fn=lambda d, day: "https://cdn.test/x.jpg",
                       caption_fn=lambda fact: "something the gate rejects",
                       logger=lambda m: None)
    assert rows == [], "a degraded post on a client's Google listing is worse than none"


def test_an_injected_caption_fn_cannot_route_around_the_gate(monkeypatch):
    # the belt in gbp_caption(): even an injected generator's output is re-gated.
    _armed(monkeypatch)
    calls = []
    monkeypatch.setattr("agent.gbp.caption_issues",
                        lambda cap, city=None: calls.append(cap) or ["nope"])
    assert gm.rows_for("eng", [_draft()], ctx=_ctx(),
                       image_fn=lambda d, day: "https://cdn.test/x.jpg",
                       caption_fn=lambda fact: "bypass attempt",
                       logger=lambda m: None) == []
    assert calls, "the gate actually ran on the injected caption"


def test_a_row_with_no_croppable_still_is_skipped(monkeypatch):
    _armed(monkeypatch)
    rows = gm.rows_for("eng", [_draft()], ctx=_ctx(),
                       image_fn=lambda d, day: None,
                       caption_fn=lambda fact: "fine copy",
                       logger=lambda m: None)
    assert rows == [], "never write a Google row with no image"


# ---- caption grounding: only ever REMOVES ---------------------------------------------

def test_source_fact_strips_hashtags_and_keeps_the_real_words():
    cap = "Members showed up at 5am.\nThat is the whole secret.\n#crossfit #gym"
    out = gm.source_fact(cap)
    assert "#" not in out
    assert "Members showed up at 5am." in out
    assert "That is the whole secret." in out


def test_source_fact_strips_a_trailing_inline_hashtag_run():
    assert gm.source_fact("Real words here. #one #two") == "Real words here."


def test_source_fact_returns_empty_when_there_was_nothing_but_hashtags():
    assert gm.source_fact("#one #two") == ""
    assert gm.source_fact("") == ""
    assert gm.source_fact(None) == ""


def test_a_caption_with_no_real_facts_never_reaches_the_generator(monkeypatch):
    _armed(monkeypatch)
    called = []
    rows = gm.rows_for("eng", [_draft(caption="#justhashtags #nothingreal")],
                       ctx=_ctx(), image_fn=lambda d, day: "https://cdn.test/x.jpg",
                       caption_fn=lambda fact: called.append(fact) or "copy",
                       logger=lambda m: None)
    assert rows == [] and called == []


# ---- connected gyms only ---------------------------------------------------------------

def test_a_gym_that_is_not_connected_gets_no_rows(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr("agent.gbp_dogfood._connection_status",
                        lambda base, store: "needs_reconnect")
    fake_store = types.SimpleNamespace(connections_for=lambda b: [],
                                       onboarding_intake=lambda b: {})
    assert gm.rows_for("eng", [_draft()], store=fake_store,
                       logger=lambda m: None) == []


def test_a_store_that_cannot_read_the_connection_is_a_loud_wiring_bug_not_silence(
        monkeypatch):
    # passing the plain calendar store (no connections_for) must NOT read as "no gym is
    # connected" for every gym -- that is the silent-no-op class this repo has been
    # burned by. It logs a wiring-bug line and writes nothing.
    _armed(monkeypatch)
    seen = []
    assert gm.rows_for("eng", [_draft()], store=types.SimpleNamespace(),
                       logger=lambda m: seen.append(m)) == []
    assert any("wiring bug" in m for m in seen)


def test_no_readable_city_means_no_rows_never_a_guessed_location(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr("agent.gbp_dogfood._connection_status",
                        lambda base, store: "connected")
    fake_store = types.SimpleNamespace(connections_for=lambda b: [],
                                       onboarding_intake=lambda b: {})
    ctx = gm.resolve_context("eng", store=fake_store,
                             address_fn=lambda b: "no commas here",
                             logger=lambda m: None)
    assert ctx is None


def test_missing_voice_blocks_rather_than_fabricating(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr("agent.gbp_dogfood._connection_status",
                        lambda base, store: "connected")
    monkeypatch.setattr("agent.gbp_dogfood._resolve_voice", lambda base: None)
    fake_store = types.SimpleNamespace(connections_for=lambda b: [],
                                       onboarding_intake=lambda b: {})
    ctx = gm.resolve_context("eng", store=fake_store,
                             address_fn=lambda b: "1 A St, Cape Coral, Florida",
                             logger=lambda m: None)
    assert ctx is None


# ---- isolation: the mirror never sinks the real month ---------------------------------

def test_hosting_off_means_no_rows_and_no_crash(monkeypatch):
    monkeypatch.setenv("AGENT_GBP_MIRROR", "true")
    monkeypatch.setattr(config, "hosting_enabled", lambda: False)
    assert gm.rows_for("eng", [_draft()], ctx=_ctx(), logger=lambda m: None) == []


def test_an_exploding_crop_never_raises_into_the_build(monkeypatch):
    _armed(monkeypatch)

    def _boom(d, day):
        raise RuntimeError("crop exploded")

    assert gm.rows_for("eng", [_draft()], ctx=_ctx(), image_fn=_boom,
                       caption_fn=lambda f: "copy", logger=lambda m: None) == []


def test_an_exploding_context_resolve_never_raises_into_the_build(monkeypatch):
    _armed(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(gm, "resolve_context", _boom)
    assert gm.rows_for("eng", [_draft()], logger=lambda m: None) == []
