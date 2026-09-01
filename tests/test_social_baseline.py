"""tests/test_social_baseline.py — BEFORE/AFTER social metrics
(agent/social_baseline.py, flag AGENT_SOCIAL_BASELINE).

All offline: fixture Apify posts, fake stores, fake HTTP. Covers:
  - compute_measures against fixtures hitting EVERY rubric measure (ask
    detection, duplicate captions, gap math incl. window edges, reels share,
    plays = videoPlayCount reels-only, medians, honest nulls, pinned excluded)
  - echo_start_date: published-first, any-status fallback, honest None
  - baseline immutability: an existing row is never recaptured or overwritten
  - inert without APIFY_TOKEN (clear reason, no crash, no network)
  - no-handle honest skip
  - before_after windows + per-measure deltas with honest nulls
  - CLI table output + flag gate
  - store readers (first_calendar_date, social_connection_handle) params
  - the monthly retro digest carries the SINCE ECHO STARTED block (flag ON)
    and omits it (flag OFF)
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import social_baseline as sb
from agent.portal_calendar_store import SupabaseCalendarStore


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _post(day, *, caption="", ptype="Image", product="feed", plays=None,
          pinned=False):
    p = {
        "timestamp": f"{day}T12:00:00.000Z",
        "type": ptype,
        "productType": product,
        "caption": caption,
        "isPinned": pinned,
    }
    if plays is not None:
        p["videoPlayCount"] = plays
    return p


# Window 2026-01-01 .. 2026-01-31 (30 days), 4 in-window posts:
#   01-05 reel  1000 plays, caption with an ask
#   01-06 photo,           caption "Great vibes today"
#   01-20 reel   200 plays, caption "Great vibes today"   (duplicate)
#   01-25 reel  no play count (honest exclusion from plays)
# plus a pinned post and two out-of-window posts that must all be ignored.
FIXTURE_POSTS = [
    _post("2026-01-05", ptype="Video", product="clips", plays=1000,
          caption="DM us to get started with a free intro!"),
    _post("2026-01-06", caption="Great vibes today"),
    _post("2026-01-20", ptype="Video", product="clips", plays=200,
          caption="Great  Vibes   Today"),
    _post("2026-01-25", ptype="Video", product="clips",
          caption="new reel no counts"),
    _post("2026-01-10", pinned=True, caption="pinned must not count"),
    _post("2025-12-25", caption="before the window"),
    _post("2026-02-02", caption="after the window"),
]

WS, WE = date(2026, 1, 1), date(2026, 1, 31)


class FakeApify:
    """Stands in for ApifyClient: fixed token + canned posts, records calls."""

    def __init__(self, posts=None, token="tok"):
        self._posts = posts if posts is not None else []
        self._token = token
        self.calls = []

    def token(self):
        return self._token

    def fetch_posts(self, handle, newer_than_days, results_limit=500):
        self.calls.append((handle, newer_than_days, results_limit))
        return list(self._posts)


class FakeCalStore:
    def __init__(self, handle="enggym", first_published=None, first_any=None):
        self._handle = handle
        self._first_published = first_published
        self._first_any = first_any

    def social_connection_handle(self, gym, platform="instagram"):
        return self._handle

    def first_calendar_date(self, gym, status=None):
        return self._first_published if status == "published" else self._first_any


class FakeBaselineStore:
    def __init__(self, row=None, conflict=False):
        self.row = row
        self.conflict = conflict
        self.inserted = []

    def get(self, gym_id):
        return self.row

    def insert_once(self, row):
        if self.conflict:
            return False, "baseline already captured (immutable); refusing overwrite"
        self.inserted.append(row)
        return True, "stored"


# ---------------------------------------------------------------------------
# compute_measures: the whole rubric
# ---------------------------------------------------------------------------

def test_compute_measures_full_rubric():
    m = sb.compute_measures(FIXTURE_POSTS, WS, WE)
    assert m["posts_count"] == 4                       # pinned + out-of-window dropped
    assert m["window_days"] == 30
    assert m["posts_per_week"] == round(4 / (30 / 7), 2)
    # gaps incl. edges: start->05 = 4, 05->06 = 1, 06->20 = 14, 20->25 = 5,
    # 25->end = 6  =>  longest 14
    assert m["longest_gap_days"] == 14
    assert m["reels_share_pct"] == 75.0                # 3 of 4
    # plays: reels only, KNOWN counts only (the third reel has none)
    assert m["total_video_plays"] == 1200
    assert m["median_plays_per_reel"] == 600.0
    # asks: only the DM/free intro caption matches
    assert m["posts_with_ask"] == 1
    assert m["ask_pct"] == 25.0
    # duplicates: "Great vibes today" normalized == "great  vibes   today"
    assert m["duplicate_caption_count"] == 1
    caps = sorted(len(c) for c in [
        "DM us to get started with a free intro!", "Great vibes today",
        "Great  Vibes   Today", "new reel no counts"])
    assert m["median_caption_length"] == (caps[1] + caps[2]) / 2.0
    assert m["window_start"] == "2026-01-01" and m["window_end"] == "2026-01-31"


def test_compute_measures_empty_window_honest_nulls():
    m = sb.compute_measures([], WS, WE)
    assert m["posts_count"] == 0
    assert m["posts_per_week"] == 0.0
    assert m["longest_gap_days"] == 30                 # the whole window is dark
    assert m["reels_share_pct"] is None
    assert m["total_video_plays"] is None
    assert m["median_plays_per_reel"] is None
    assert m["median_caption_length"] is None
    assert m["posts_with_ask"] == 0
    assert m["ask_pct"] is None
    assert m["duplicate_caption_count"] == 0


def test_compute_measures_no_reels_plays_are_null_not_zero():
    posts = [_post("2026-01-05", caption="a"), _post("2026-01-06", caption="b")]
    m = sb.compute_measures(posts, WS, WE)
    assert m["reels_share_pct"] == 0.0
    assert m["total_video_plays"] is None
    assert m["median_plays_per_reel"] is None


def test_compute_measures_empty_captions_never_duplicate():
    posts = [_post("2026-01-05"), _post("2026-01-06"), _post("2026-01-07", caption="  ")]
    m = sb.compute_measures(posts, WS, WE)
    assert m["duplicate_caption_count"] == 0


def test_compute_measures_rejects_bad_window():
    with pytest.raises(ValueError):
        sb.compute_measures([], WE, WS)


def test_ask_regex_matches_report_card_phrases():
    for cap in ("Link in bio!", "book your no sweat intro", "Sign up today",
                "text us at 555", "join us Saturday"):
        assert sb.ASK_RE.search(cap), cap
    assert not sb.ASK_RE.search("great workout this morning")


# ---------------------------------------------------------------------------
# echo_start_date
# ---------------------------------------------------------------------------

def test_echo_start_prefers_first_published():
    store = FakeCalStore(first_published="2026-06-01", first_any="2026-05-15")
    assert sb.echo_start_date("eng", store=store) == date(2026, 6, 1)


def test_echo_start_falls_back_to_first_row():
    store = FakeCalStore(first_published=None, first_any="2026-05-15")
    assert sb.echo_start_date("eng", store=store) == date(2026, 5, 15)


def test_echo_start_honest_none_without_history():
    store = FakeCalStore(first_published=None, first_any=None)
    assert sb.echo_start_date("eng", store=store) is None


# ---------------------------------------------------------------------------
# capture_baseline
# ---------------------------------------------------------------------------

def test_capture_inert_without_token(monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    out = sb.capture_baseline("eng", client=sb.ApifyClient(token=""),
                              store=FakeCalStore(),
                              baseline_store=FakeBaselineStore())
    assert out["ok"] is False
    assert "APIFY_TOKEN not set" in out["reason"]


def test_capture_skips_without_handle():
    out = sb.capture_baseline(
        "eng", client=FakeApify(),
        store=FakeCalStore(handle=None, first_published="2026-06-01"),
        baseline_store=FakeBaselineStore(), today=date(2026, 8, 28))
    assert out["ok"] is False and "no instagram handle" in out["reason"]


def test_capture_skips_without_calendar_history():
    out = sb.capture_baseline(
        "eng", client=FakeApify(), store=FakeCalStore(first_published=None),
        baseline_store=FakeBaselineStore(), today=date(2026, 8, 28))
    assert out["ok"] is False and "echo start unknown" in out["reason"]


def test_capture_stores_the_pre_start_window_once():
    client = FakeApify(posts=FIXTURE_POSTS)
    bstore = FakeBaselineStore()
    out = sb.capture_baseline(
        "eng", client=client,
        store=FakeCalStore(first_published="2026-01-31"),
        baseline_store=bstore, today=date(2026, 8, 28))
    assert out["ok"] and out["captured"]
    row = bstore.inserted[0]
    assert row["gym_id"] == "eng"
    assert row["ig_handle"] == "enggym"
    assert row["echo_start"] == "2026-01-31"
    # BEFORE window: the 90 days ending AT echo start (end exclusive).
    assert row["window_end"] == "2026-01-31"
    assert row["window_start"] == "2025-11-02"
    assert row["measures"]["window_end"] == "2026-01-31"
    # the pull reaches back far enough to cover the whole before window
    handle, days_back, _ = client.calls[0]
    assert handle == "enggym"
    assert days_back == (date(2026, 8, 28) - date(2025, 11, 2)).days + 1
    # the before window is [2025-11-02, 2026-01-31): the four January posts
    # plus the 2025-12-25 one count; the pinned post and anything on/after the
    # echo start never leak in
    assert row["measures"]["posts_count"] == 5


def test_capture_refuses_when_baseline_exists_immutable():
    existing = {"gym_id": "eng", "ig_handle": "enggym", "measures": {}}
    client = FakeApify(posts=FIXTURE_POSTS)
    bstore = FakeBaselineStore(row=existing)
    out = sb.capture_baseline(
        "eng", client=client, store=FakeCalStore(first_published="2026-01-31"),
        baseline_store=bstore, today=date(2026, 8, 28))
    assert out["ok"] is True and out["captured"] is False
    assert "immutable" in out["reason"]
    assert bstore.inserted == []          # nothing written
    assert client.calls == []             # nothing even pulled


def test_capture_race_dies_on_insert_conflict():
    bstore = FakeBaselineStore(conflict=True)
    out = sb.capture_baseline(
        "eng", client=FakeApify(posts=FIXTURE_POSTS),
        store=FakeCalStore(first_published="2026-01-31"),
        baseline_store=bstore, today=date(2026, 8, 28))
    assert out["ok"] is False and "immutable" in out["reason"]


def test_capture_refuses_future_echo_start():
    out = sb.capture_baseline(
        "eng", client=FakeApify(),
        store=FakeCalStore(first_published="2026-12-01"),
        baseline_store=FakeBaselineStore(), today=date(2026, 8, 28))
    assert out["ok"] is False and "future" in out["reason"]


# ---------------------------------------------------------------------------
# before_after
# ---------------------------------------------------------------------------

def _baseline_row():
    return {
        "gym_id": "eng", "ig_handle": "enggym", "echo_start": "2026-01-31",
        "window_start": "2025-11-02", "window_end": "2026-01-31",
        "measures": {
            "posts_count": 4, "posts_per_week": 0.31, "longest_gap_days": 40,
            "reels_share_pct": 25.0, "total_video_plays": 500,
            "median_plays_per_reel": 500.0, "median_caption_length": 18.0,
            "posts_with_ask": 0, "ask_pct": 0.0, "duplicate_caption_count": 2,
            "window_start": "2025-11-02", "window_end": "2026-01-31",
            "window_days": 90,
        },
    }


def test_before_after_windows_and_deltas():
    today = date(2026, 8, 28)
    after_posts = [
        _post("2026-08-01", ptype="Video", product="clips", plays=3000,
              caption="Book your free intro, link in bio"),
        _post("2026-08-10", ptype="Video", product="clips", plays=1000,
              caption="Meet coach Sam"),
    ]
    client = FakeApify(posts=after_posts)
    out = sb.before_after("eng", client=client,
                          baseline_store=FakeBaselineStore(row=_baseline_row()),
                          today=today)
    assert out["ok"]
    assert out["handle"] == "enggym"       # the STORED handle: same feed both sides
    a = out["after"]
    assert a["window_end"] == "2026-08-28" and a["window_start"] == "2026-05-30"
    assert a["posts_count"] == 2
    assert a["total_video_plays"] == 4000
    d = out["deltas"]
    assert d["posts_count"] == -2
    assert d["total_video_plays"] == 3500
    assert d["posts_with_ask"] == 1
    # the after pull asked for the last 90 days (+1 buffer)
    assert client.calls[0][1] == 91


def test_before_after_honest_null_deltas():
    row = _baseline_row()
    row["measures"]["total_video_plays"] = None       # before had no reels
    row["measures"]["median_plays_per_reel"] = None
    out = sb.before_after("eng", client=FakeApify(posts=[]),
                          baseline_store=FakeBaselineStore(row=row),
                          today=date(2026, 8, 28))
    assert out["ok"]
    assert out["deltas"]["total_video_plays"] is None
    assert out["deltas"]["median_plays_per_reel"] is None
    # after window empty: reels share None on the after side -> delta None
    assert out["deltas"]["reels_share_pct"] is None


def test_before_after_requires_a_stored_baseline():
    out = sb.before_after("eng", client=FakeApify(),
                          baseline_store=FakeBaselineStore(row=None),
                          today=date(2026, 8, 28))
    assert out["ok"] is False and "no baseline captured" in out["reason"]


def test_before_after_inert_without_token(monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    out = sb.before_after("eng", client=sb.ApifyClient(token=""),
                          baseline_store=FakeBaselineStore(row=_baseline_row()))
    assert out["ok"] is False and "APIFY_TOKEN not set" in out["reason"]


# ---------------------------------------------------------------------------
# Apify client contract (offline, fake http)
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, post_resp=None, get_resp=None):
        self.calls = []
        self._post = post_resp or _Resp(200, [])
        self._get = get_resp or _Resp(200, [])

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("post", url, params or {}, json))
        return self._post

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("get", url, params or {}, None))
        return self._get


def test_apify_payload_matches_the_proven_contract():
    http = _FakeHTTP(post_resp=_Resp(200, []))
    client = sb.ApifyClient(token="tok", http=http)
    client.fetch_posts("@EngGym", 273)
    _, url, params, payload = http.calls[0]
    assert "instagram-post-scraper" in url and "run-sync-get-dataset-items" in url
    assert params == {"token": "tok"}
    assert payload["username"] == ["EngGym"]          # @ stripped
    assert payload["skipPinnedPosts"] is True
    assert payload["onlyPostsNewerThan"] == "273 days"
    assert payload["dataDetailLevel"] == "detailedData"   # exact value required
    assert payload["resultsLimit"] == sb.RESULTS_LIMIT


def test_apify_error_never_leaks_the_token():
    http = _FakeHTTP(post_resp=_Resp(500, [], text="boom token=sekret123 died"))
    client = sb.ApifyClient(token="sekret123", http=http)
    with pytest.raises(sb.ApifyError) as exc:
        client.fetch_posts("enggym", 90)
    assert "sekret123" not in str(exc.value)


# ---------------------------------------------------------------------------
# store readers (SupabaseCalendarStore additions)
# ---------------------------------------------------------------------------

class _RoutedHTTP:
    """Fake requests: routes GETs by table in the URL."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params or {}))
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return _Resp(200, [])


def test_first_calendar_date_params_and_result():
    http = _RoutedHTTP({"content_calendar": _Resp(200, [{"post_date": "2026-06-01"}])})
    store = SupabaseCalendarStore(url="https://x.supabase.co", service_key="k", http=http)
    got = store.first_calendar_date("eng", status="published")
    assert got == "2026-06-01"
    _, params = http.calls[0]
    assert params["gym_id"] == "eq.eng"
    assert params["status"] == "eq.published"
    assert params["order"] == "post_date.asc" and params["limit"] == "1"


def test_first_calendar_date_none_when_empty():
    http = _RoutedHTTP({"content_calendar": _Resp(200, [])})
    store = SupabaseCalendarStore(url="https://x.supabase.co", service_key="k", http=http)
    assert store.first_calendar_date("eng") is None


def test_social_connection_handle_reads_live_truth():
    http = _RoutedHTTP({
        "gyms": _Resp(200, [{"id": "uuid-1", "slug": "eng", "name": "ENG"}]),
        "echo_social_connections": _Resp(200, [{"handle": "enggym"}]),
    })
    store = SupabaseCalendarStore(url="https://x.supabase.co", service_key="k", http=http)
    assert store.social_connection_handle("eng") == "enggym"
    conn_call = [c for c in http.calls if "echo_social_connections" in c[0]][0]
    assert conn_call[1]["gym_id"] == "eq.uuid-1"
    assert conn_call[1]["platform"] == "eq.instagram"


def test_social_connection_handle_none_when_gym_unknown():
    http = _RoutedHTTP({"gyms": _Resp(200, [])})
    store = SupabaseCalendarStore(url="https://x.supabase.co", service_key="k", http=http)
    assert store.social_connection_handle("ghost") is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_flag_off_is_inert(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_SOCIAL_BASELINE", raising=False)
    rc = sb.cli(["--gym", "eng"])
    out = capsys.readouterr().out
    assert rc == 1 and "AGENT_SOCIAL_BASELINE" in out


def test_cli_requires_a_target(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_SOCIAL_BASELINE", "true")
    rc = sb.cli([])
    assert rc == 1 and "usage" in capsys.readouterr().out


def test_cli_prints_the_before_after_table(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_SOCIAL_BASELINE", "true")
    after_posts = [_post("2026-08-01", ptype="Video", product="clips", plays=3000,
                         caption="Book your free intro")]
    rc = sb.cli(["--gym", "eng"], client=FakeApify(posts=after_posts),
                store=FakeCalStore(),
                baseline_store=FakeBaselineStore(row=_baseline_row()),
                today=date(2026, 8, 28))
    out = capsys.readouterr().out
    assert rc == 0
    assert "eng (@enggym)" in out and "echo start 2026-01-31" in out
    assert "before" in out and "after" in out and "delta" in out
    assert "video plays (total)" in out
    assert "plays = videoPlayCount, reels only" in out


def test_cli_capture_then_report(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_SOCIAL_BASELINE", "true")

    class _CaptureStore(FakeBaselineStore):
        def insert_once(self, row):
            ok, reason = super().insert_once(row)
            self.row = row                      # the report then finds it
            return ok, reason

    rc = sb.cli(["--gym", "eng", "--capture"],
                client=FakeApify(posts=FIXTURE_POSTS),
                store=FakeCalStore(first_published="2026-01-31"),
                baseline_store=_CaptureStore(),
                today=date(2026, 8, 28))
    out = capsys.readouterr().out
    assert rc == 0
    assert "capture eng: stored" in out
    assert "eng (@enggym)" in out


def test_cli_honest_skip_shows_the_reason(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_SOCIAL_BASELINE", "true")
    rc = sb.cli(["--gym", "eng"], client=FakeApify(),
                store=FakeCalStore(),
                baseline_store=FakeBaselineStore(row=None),
                today=date(2026, 8, 28))
    out = capsys.readouterr().out
    assert rc == 1 and "no baseline captured" in out


# ---------------------------------------------------------------------------
# --all coverage: the fleet the sweep actually reaches (audit 2026-08-31)
#
# Coverage stood at 3 of the fleet because --all was built from
# client_gym_bases() alone, which EXCLUDES the LASSO tenant (it has its own
# Meta-direct PUBLISHING lane). Measurement is not publishing: LASSO's public
# feed has the same before/after story, so the documented command could never
# capture it. And one gym's exception aborted the whole run, leaving every gym
# after it silently uncaptured on a manual rail that has no retry.
# ---------------------------------------------------------------------------

class _FakeAccount:
    def __init__(self, key, platform):
        self.key = key
        self.platform = platform


def _patch_fleet(monkeypatch, bases, accounts):
    import agent.calendar_autopublish as cap
    import agent.accounts as accounts_mod
    monkeypatch.setattr(cap, "client_gym_bases", lambda: list(bases))
    monkeypatch.setattr(accounts_mod, "all_accounts", lambda: list(accounts))


def test_all_baseline_gyms_includes_lasso(monkeypatch):
    _patch_fleet(monkeypatch, ["eng", "gritx"], [
        _FakeAccount("lasso_ig", "instagram"),
        _FakeAccount("eng_ig", "instagram"),
        _FakeAccount("gritx_fb", "facebook_page"),
    ])
    assert sb.all_baseline_gyms() == ["lasso", "eng", "gritx"]


def test_all_baseline_gyms_drops_non_social_registry_keys(monkeypatch):
    _patch_fleet(monkeypatch, ["eng", "blake_personal"], [
        _FakeAccount("lasso_ig", "instagram"),
        _FakeAccount("eng_ig", "instagram"),
        _FakeAccount("blake_personal", "personal"),
    ])
    assert sb.all_baseline_gyms() == ["lasso", "eng"]


def test_all_baseline_gyms_dedupes_and_survives_an_unreadable_registry(monkeypatch):
    import agent.calendar_autopublish as cap
    import agent.accounts as accounts_mod

    def _boom():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(cap, "client_gym_bases", lambda: ["lasso", "eng", "eng"])
    monkeypatch.setattr(accounts_mod, "all_accounts", _boom)
    # No platform filtering rather than a silently shrunken fleet.
    assert sb.all_baseline_gyms() == ["lasso", "eng"]


def test_cli_all_captures_lasso_too(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_SOCIAL_BASELINE", "true")
    _patch_fleet(monkeypatch, ["eng"], [
        _FakeAccount("lasso_ig", "instagram"),
        _FakeAccount("eng_ig", "instagram"),
    ])

    class _CaptureStore(FakeBaselineStore):
        def insert_once(self, row):
            ok, reason = super().insert_once(row)
            return ok, reason

    store = _CaptureStore()
    sb.cli(["--all", "--capture"], client=FakeApify(posts=FIXTURE_POSTS),
           store=FakeCalStore(first_published="2026-01-31"),
           baseline_store=store, today=date(2026, 8, 28))
    out = capsys.readouterr().out
    assert "capture lasso: stored" in out
    assert "capture eng: stored" in out
    assert [r["gym_id"] for r in store.inserted] == ["lasso", "eng"]


def test_cli_all_one_gym_exploding_does_not_abort_the_sweep(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_SOCIAL_BASELINE", "true")
    _patch_fleet(monkeypatch, ["boom", "eng"], [
        _FakeAccount("lasso_ig", "instagram"),
        _FakeAccount("boom_ig", "instagram"),
        _FakeAccount("eng_ig", "instagram"),
    ])

    class _ExplodingStore(FakeBaselineStore):
        def get(self, gym_id):
            if gym_id == "boom":
                # A raw client error: its message can carry the request URL,
                # and the Apify token rides in that query string.
                raise RuntimeError("HTTPSConnectionPool ... ?token=SUPERSECRET")
            return self.row

    rc = sb.cli(["--all", "--capture"], client=FakeApify(posts=FIXTURE_POSTS),
                store=FakeCalStore(first_published="2026-01-31"),
                baseline_store=_ExplodingStore(), today=date(2026, 8, 28))
    out = capsys.readouterr().out
    assert rc == 1                                   # the failure is reported
    assert "capture boom: capture failed: RuntimeError" in out
    assert "SUPERSECRET" not in out                  # never the message
    # The gyms AFTER the failure still ran — that is the whole fix.
    assert "capture lasso: stored" in out
    assert "capture eng: stored" in out


# ---------------------------------------------------------------------------
# since-echo digest block + monthly retro wiring
# ---------------------------------------------------------------------------

def test_since_echo_lines_flag_off_is_none(monkeypatch):
    monkeypatch.delenv("AGENT_SOCIAL_BASELINE", raising=False)
    assert sb.since_echo_lines("eng", result={"ok": True}) is None


def test_since_echo_lines_formats_the_block(monkeypatch):
    monkeypatch.setenv("AGENT_SOCIAL_BASELINE", "true")
    result = sb.before_after(
        "eng",
        client=FakeApify(posts=[_post("2026-08-01", ptype="Video",
                                      product="clips", plays=3000,
                                      caption="Book a free intro")]),
        baseline_store=FakeBaselineStore(row=_baseline_row()),
        today=date(2026, 8, 28))
    lines = sb.since_echo_lines("eng", result=result)
    assert lines[0].startswith("SINCE ECHO STARTED")
    assert "@enggym" in lines[0] and "echo start 2026-01-31" in lines[0]
    joined = "\n".join(lines)
    assert "video plays (total): 500 -> 3000 (+2500)" in joined
    assert "posts: 4 -> 1 (-3)" in joined


def test_since_echo_lines_not_ok_is_none(monkeypatch):
    monkeypatch.setenv("AGENT_SOCIAL_BASELINE", "true")
    assert sb.since_echo_lines(
        "eng", result={"ok": False, "reason": "no baseline"}) is None


def test_monthly_retro_digest_carries_since_echo_block(monkeypatch):
    from datetime import datetime, timezone
    from tests.test_monthly_retro import FakeRetroStore, _month_rows
    from agent.jobs import monthly_retro

    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    monkeypatch.setenv("AGENT_SOCIAL_BASELINE", "true")
    store = FakeRetroStore({"2026-08": _month_rows("2026-08")})
    store.since_echo_block = lambda gym_id: [
        "SINCE ECHO STARTED (Instagram @enggym, echo start 2026-01-31; the 90 "
        "days before vs the last 90, public feed via Apify; plays = "
        "videoPlayCount, reels only):",
        "  posts: 4 -> 38 (+34)",
    ]
    out = monthly_retro.run(month="2026-08", gyms=["gym1"], store=store,
                            now=datetime(2026, 9, 5, tzinfo=timezone.utc),
                            notifier=lambda g, t: None)
    digest = out["gyms"][0]["digest"]
    assert "SINCE ECHO STARTED" in digest
    assert "posts: 4 -> 38 (+34)" in digest


def test_monthly_retro_flag_off_no_since_echo_block(monkeypatch):
    from datetime import datetime, timezone
    from tests.test_monthly_retro import FakeRetroStore, _month_rows
    from agent.jobs import monthly_retro

    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    monkeypatch.delenv("AGENT_SOCIAL_BASELINE", raising=False)
    store = FakeRetroStore({"2026-08": _month_rows("2026-08")})
    called = []
    store.since_echo_block = lambda gym_id: called.append(gym_id) or ["x"]
    out = monthly_retro.run(month="2026-08", gyms=["gym1"], store=store,
                            now=datetime(2026, 9, 5, tzinfo=timezone.utc),
                            notifier=lambda g, t: None)
    assert "SINCE ECHO STARTED" not in out["gyms"][0]["digest"]
    assert called == []                    # flag OFF: never even consulted
