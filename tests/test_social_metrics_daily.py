"""
Regression tests for the daily follower series pipeline (AUD-007 / D1), the C15 token
health read, the AUD-005 account-id stamp, and the null semantics in
docs/METRICS_DATA_CONTRACT.md.

Fixtures are real-shape, captured from api.zernio.com on 2026-09-05.
"""

from agent import social_metrics_daily as smd
from agent import zernio as z


# ---- real-shape fixtures ----------------------------------------------------

ENG_IG = "6a784c5cd0fe733d1a742da3"
GBP_ENG = "6a84d07c77555aae01c8b686"

STATS = {
    "accounts": [
        {"_id": ENG_IG, "platform": "instagram", "username": "thecrossfiteng.official",
         "currentFollowers": 996, "growth": 30, "dataPoints": 27},
        {"_id": GBP_ENG, "platform": "googlebusiness", "username": "CrossFit ENG",
         "currentFollowers": 0, "growth": 0, "dataPoints": 0},
    ],
    "stats": {
        ENG_IG: [{"date": "2026-08-10", "followers": 966},
                 {"date": "2026-08-11", "followers": 964},
                 {"date": "2026-09-04", "followers": 995},
                 {"date": "2026-09-05", "followers": 996}],
        GBP_ENG: [],
    },
    "dateRange": {"from": "2025-01-01T00:00:00.000Z", "to": "2026-09-05T23:59:59.999Z"},
    "granularity": "daily",
}

GYM = "11111111-2222-3333-4444-555555555555"

CONNS = [
    {"gym_id": GYM, "platform": "instagram", "state": "connected",
     "handle": "thecrossfiteng.official", "late_account_id": ENG_IG},
    {"gym_id": GYM, "platform": "googlebusiness", "state": "connected",
     "handle": "CrossFit ENG", "late_account_id": GBP_ENG},
]

# /v1/accounts/health, exact live shape 2026-09-05 13:44 UTC. Top Fuel's googlebusiness
# tokenExpiresAt was 37 minutes IN THE PAST and the account was healthy and postable.
HEALTH = {
    "summary": {"total": 3, "healthy": 3, "warning": 0, "error": 0, "needsReconnect": 0},
    "accounts": [
        {"accountId": "gbp-topfuel", "platform": "googlebusiness",
         "username": "Top Fuel CrossFit", "profileId": "p1", "status": "healthy",
         "canPost": True, "canFetchAnalytics": True, "analyticsSupported": False,
         "tokenValid": True, "needsReconnect": False, "issues": [],
         "tokenExpiresAt": "2026-09-05T13:07:55.410Z"},
        {"accountId": ENG_IG, "platform": "instagram",
         "username": "thecrossfiteng.official", "profileId": "p2", "status": "healthy",
         "canPost": True, "tokenValid": True, "needsReconnect": False, "issues": [],
         "tokenExpiresAt": "2026-09-05T14:27:19.414Z"},
        {"accountId": "broken", "platform": "facebook", "username": "x", "profileId": "p3",
         "status": "error", "canPost": False, "tokenValid": False,
         "needsReconnect": True, "issues": ["token revoked"],
         "tokenExpiresAt": "2026-12-01T00:00:00.000Z"},
    ],
}


# ---- C15: tokenExpiresAt is not a reconnect signal ---------------------------

def test_c15_a_past_token_expiry_is_not_a_reconnect_when_the_token_is_valid():
    """The whole of C15. Alerting on tokenExpiresAt would have opened 13 client tickets
    for a system that was working: Google access tokens live an hour and Zernio refreshes
    them behind the call."""
    read = z.token_health_read(HEALTH)
    top = read["gbp-topfuel"]
    assert top["token_expires_at"] == "2026-09-05T13:07:55.410Z"
    assert top["needs_reconnect"] is False
    assert top["token_valid"] is True


def test_c15_a_future_token_expiry_is_still_a_reconnect_when_the_token_is_invalid():
    """The other direction: expiry is not the signal either way. This row's token does not
    expire until December and it still needs a reconnect."""
    read = z.token_health_read(HEALTH)
    assert read["broken"]["needs_reconnect"] is True
    assert read["broken"]["issues"] == ["token revoked"]


def test_c15_health_read_covers_every_account_with_an_id():
    assert set(z.token_health_read(HEALTH)) == {"gbp-topfuel", ENG_IG, "broken"}


def test_token_health_read_is_defensive_on_garbage():
    for bad in (None, {}, {"accounts": None}, {"accounts": [None, 3, "x", {}]}):
        assert z.token_health_read(bad) == {}


# ---- AUD-005: the account id comes off the connected account -----------------

def test_map_account_ids_returns_the_connected_account_id_per_platform():
    accounts = {"accounts": [
        {"_id": "ig1", "platform": "instagram",
         "metadata": {"profileData": {"username": "eng"}}},
        {"_id": "gbp1", "platform": "googlebusiness",
         "metadata": {"locationName": "CrossFit ENG"}},
    ]}
    assert z.map_account_ids(accounts) == {"instagram": "ig1", "googlebusiness": "gbp1"}


def test_map_account_ids_skips_an_expired_account():
    """An expired account must never be handed to the metrics pull as if it were live."""
    accounts = {"accounts": [
        {"_id": "ig1", "platform": "instagram", "isActive": False},
    ]}
    assert z.map_account_ids(accounts) == {}


def test_map_status_shape_is_unchanged_by_the_new_mapper():
    """map_status IS the portal status contract. Guard it stays byte identical."""
    out = z.map_status({"accounts": [
        {"_id": "ig1", "platform": "instagram",
         "metadata": {"profileData": {"username": "eng"}}}]})
    assert out["platforms"]["instagram"] == {
        "connected": True, "handle": "eng", "expired": False, "expires_at": None}


# ---- AUD-007 / D1: the series is built -------------------------------------

def test_build_rows_produces_one_row_per_measured_day():
    rows = smd.build_rows(CONNS, STATS, backfill_days=0)
    ig = [r for r in rows if r["platform"] == "instagram"]
    assert len(ig) == 4
    assert {r["metric_date"] for r in ig} == {
        "2026-08-10", "2026-08-11", "2026-09-04", "2026-09-05"}
    assert all(r["gym_id"] == GYM and r["late_account_id"] == ENG_IG for r in ig)


def test_every_row_carries_provenance():
    """No fabrication: a number with no provenance never reaches a client report."""
    rows = smd.build_rows(CONNS, STATS, backfill_days=0)
    assert rows
    for r in rows:
        prov = r["raw"]["_provenance"]
        assert prov["source"] == "zernio"
        assert prov["endpoint"] == "/v1/accounts/follower-stats"
        assert prov["account_id"] == r["late_account_id"]
        assert prov["fetched_at"] == r["pulled_at"]
        assert r["source"] == "zernio"


def test_googlebusiness_never_writes_a_fabricated_zero_follower_count():
    """Zernio reports currentFollowers 0 / dataPoints 0 for googlebusiness. That 0 is
    ABSENCE, not a measurement. NULL MEANS NULL."""
    rows = smd.build_rows(CONNS, STATS, backfill_days=0)
    assert not [r for r in rows if r["platform"] == "googlebusiness"]


def test_a_connection_with_no_account_id_is_skipped_not_guessed():
    conns = [{"gym_id": GYM, "platform": "instagram", "late_account_id": None}]
    assert smd.build_rows(conns, STATS, backfill_days=0) == []


def test_backfill_window_is_applied_locally():
    """Zernio ignores every date parameter, so the window is Echo's job."""
    import datetime
    rows = smd.build_rows(CONNS, STATS, backfill_days=3,
                          today=datetime.date(2026, 9, 5))
    assert {r["metric_date"] for r in rows} == {"2026-09-04", "2026-09-05"}


def test_run_is_off_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_SOCIAL_METRICS_DAILY", raising=False)
    out = smd.run()
    assert out["ok"] is False and out["written"] == 0
    assert "AGENT_SOCIAL_METRICS_DAILY" in out["reason"]


# ---- null semantics ---------------------------------------------------------

def test_follower_series_drops_a_missing_count_rather_than_zeroing_it():
    stats = {"stats": {"a": [
        {"date": "2026-09-01", "followers": 10},
        {"date": "2026-09-02"},                      # missing
        {"date": "2026-09-03", "followers": None},   # explicit null
        {"date": "2026-09-04", "followers": "12"},   # wrong type
        {"date": "2026-09-05", "followers": 14},
    ]}}
    assert z.follower_series(stats, "a") == [("2026-09-01", 10), ("2026-09-05", 14)]


def test_follower_series_keeps_a_genuine_zero():
    """0 reported by upstream IS a measurement. Only ABSENCE is dropped."""
    stats = {"stats": {"a": [{"date": "2026-09-01", "followers": 0}]}}
    assert z.follower_series(stats, "a") == [("2026-09-01", 0)]


def test_follower_series_dedupes_on_date_and_sorts():
    stats = {"stats": {"a": [{"date": "2026-09-05", "followers": 9},
                             {"date": "2026-09-01", "followers": 1},
                             {"date": "2026-09-05", "followers": 11}]}}
    assert z.follower_series(stats, "a") == [("2026-09-01", 1), ("2026-09-05", 11)]


# ---- the health enum --------------------------------------------------------

def _span(first, last, days=28):
    """A `days`-long series from `first` to `last`."""
    import datetime
    start = datetime.date(2026, 8, 1)
    out = []
    for i in range(days):
        v = first + round((last - first) * i / (days - 1))
        out.append(((start + datetime.timedelta(days=i)).isoformat(), v))
    return out


def test_health_read_growing():
    read, basis = z.health_read(_span(800, 900))
    assert read == "growing"
    assert basis["first"] == 800 and basis["last"] == 900
    assert basis["pct"] > 0


def test_health_read_declining():
    read, _ = z.health_read(_span(900, 800))
    assert read == "declining"


def test_health_read_flat_is_a_measured_result():
    read, basis = z.health_read(_span(1000, 1005))
    assert read == "flat"
    assert basis["points"] == 28


def test_insufficient_data_is_none_and_never_flat():
    """The single most important rule in the health enum: 'we do not know' must not
    render as 'this account is not growing'."""
    read, basis = z.health_read([("2026-09-01", 500)])
    assert read is None
    assert basis["points"] == 1


def test_a_short_span_is_none_and_never_flat():
    read, basis = z.health_read([("2026-09-01", 500), ("2026-09-03", 500)])
    assert read is None
    assert basis["points"] == 2


def test_no_points_at_all_is_none():
    read, basis = z.health_read([])
    assert read is None and basis["points"] == 0


def test_a_zero_baseline_is_none_not_infinite_growth():
    read, _ = z.health_read(_span(0, 50))
    assert read is None


def test_health_for_ignores_rows_with_a_null_follower_count():
    rows = [{"gym_id": GYM, "platform": "instagram", "metric_date": d, "followers": f}
            for d, f in _span(800, 900)]
    rows.append({"gym_id": GYM, "platform": "instagram",
                 "metric_date": "2026-09-01", "followers": None})
    out = smd.health_for(rows)
    assert out[(GYM, "instagram")][0] == "growing"


def test_health_for_a_gym_with_only_null_followers_is_none_not_flat():
    rows = [{"gym_id": GYM, "platform": "instagram",
             "metric_date": "2026-09-0%d" % i, "followers": None} for i in range(1, 6)]
    assert smd.health_for(rows) == {}


# ---- the on_conflict target must match the LIVE constraint ------------------

def test_the_upsert_targets_the_constraint_that_actually_exists():
    """Probed live 2026-09-05: (late_account_id, metric_date) resolves, while
    (gym_id, late_account_id, metric_date) returns 42P10 "there is no unique or exclusion
    constraint matching the ON CONFLICT specification" and would 400 on EVERY write. A
    draft of this pipeline targeted the second one. This test is the rail that keeps the
    writer pointed at the constraint the database really has."""
    import agent.portal_calendar_store as pcs

    seen = {}

    class _Http:
        def post(self, url, params=None, headers=None, json=None, timeout=None):
            seen["target"] = (params or {}).get("on_conflict")
            seen["table"] = url.rstrip("/").split("/rest/v1/")[-1]

            class _R:
                status_code = 201
                text = ""

                @staticmethod
                def json():
                    return []
            return _R()

    store = pcs.SupabaseCalendarStore(url="https://x.supabase.co", service_key="k",
                                      http=_Http())
    store.upsert_social_metric_days(
        [{"gym_id": GYM, "late_account_id": ENG_IG, "metric_date": "2026-09-05",
          "followers": 996, "platform": "instagram", "source": "zernio"}])
    assert seen["table"] == "gym_social_metrics_daily"
    assert seen["target"] == "late_account_id,metric_date"


def test_the_writer_drops_a_none_metric_instead_of_sending_a_zero():
    """NULL MEANS NULL at the wire. merge-duplicates would otherwise overwrite a real
    measurement with an explicit null on a later partial pull."""
    import agent.portal_calendar_store as pcs

    sent = {}

    class _Http:
        def post(self, url, params=None, headers=None, json=None, timeout=None):
            sent["body"] = json

            class _R:
                status_code = 201
                text = ""

                @staticmethod
                def json():
                    return []
            return _R()

    store = pcs.SupabaseCalendarStore(url="https://x.supabase.co", service_key="k",
                                      http=_Http())
    store.upsert_social_metric_days(
        [{"gym_id": GYM, "late_account_id": ENG_IG, "metric_date": "2026-09-05",
          "followers": 996, "reach": None, "impressions": None, "engagement": 0}])
    row = sent["body"][0]
    assert row["followers"] == 996
    assert "reach" not in row and "impressions" not in row   # absent, NOT 0
    assert row["engagement"] == 0                            # a genuine 0 survives


# ---- P-10: the token expiry the portal status strip needs -------------------

# Real shape from api.zernio.com, 2026-09-05. IG/FB carry a genuine ~60 day grant
# expiry; Google Business carries the rolling one hour access token.
_IG_LIVE = {"_id": "ig1", "platform": "instagram", "isActive": True,
            "tokenExpiresAt": "2026-09-27T13:14:03.205Z",
            "metadata": {"connectedAt": "2026-07-29T13:14:04.205Z",
                         "expires_in": 5183999,
                         "profileData": {"username": "eng"}}}
_GBP_LIVE = {"_id": "gbp1", "platform": "googlebusiness", "isActive": True,
             "tokenExpiresAt": "2026-09-05T13:07:55.410Z",
             "metadata": {"locationName": "Top Fuel CrossFit"}}


def test_the_status_payload_now_carries_a_real_grant_expiry():
    """P-10: the portal could not build 'expires in 3 days' because no expiry existed
    anywhere in the chain. It does now, as a real timestamp."""
    out = z.map_status({"accounts": [_IG_LIVE]})
    assert out["platforms"]["instagram"]["expires_at"] == "2026-09-27T13:14:03.205000+00:00"


def test_google_business_expiry_is_always_null_on_purpose():
    """C15 as a product decision. Google's tokenExpiresAt is a rolling one hour access
    token that Zernio refreshes behind the call. Live 2026-09-05 at 13:44 UTC a third of
    the 13 Google accounts carried an expiry ALREADY IN THE PAST while healthy and
    postable. Surfacing it would put 'expires within the hour' on every Google connected
    gym's screen, all day, forever: the 13 ticket false alarm made permanent."""
    out = z.map_status({"accounts": [_GBP_LIVE]})
    assert out["platforms"]["googlebusiness"]["connected"] is True
    assert out["platforms"]["googlebusiness"]["expires_at"] is None


def test_an_unknown_expiry_is_null_never_a_guess_and_never_zero():
    out = z.map_status({"accounts": [
        {"_id": "ig1", "platform": "instagram",
         "metadata": {"profileData": {"username": "eng"}}}]})
    assert out["platforms"]["instagram"]["expires_at"] is None


def test_a_missing_platform_reports_a_null_expiry_not_an_absent_key():
    """The portal reads the key unconditionally; it must always be present."""
    out = z.map_status({"accounts": []})
    for plat in z.STATUS_PLATFORMS:
        assert out["platforms"][plat]["expires_at"] is None


def test_the_expiry_can_be_derived_from_connected_at_plus_expires_in():
    acct = {"_id": "ig1", "platform": "instagram",
            "metadata": {"connectedAt": "2026-07-29T13:14:04.205Z",
                         "expires_in": 5183999,
                         "profileData": {"username": "eng"}}}
    assert z.expiry_of(acct) == "2026-09-27T13:14:03.205000+00:00"


def test_expiry_of_is_defensive_on_garbage():
    for bad in (None, {}, "x", 3, {"platform": "instagram", "tokenExpiresAt": "nonsense"},
                {"platform": "instagram", "metadata": {"expires_in": True}}):
        assert z.expiry_of(bad) is None


def test_an_expired_account_still_reports_when_it_lapsed():
    """An amber row is the one a gym owner most needs a date on."""
    acct = dict(_IG_LIVE, isActive=False)
    out = z.map_status({"accounts": [acct]})
    assert out["platforms"]["instagram"]["expired"] is True
    assert out["platforms"]["instagram"]["expires_at"] == "2026-09-27T13:14:03.205000+00:00"
