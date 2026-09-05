"""
AUD-005 regression tests: echo_social_connections is the source of truth for who is
connected, and it now carries the Zernio account id so nothing has to consult the legacy
gym_social_accounts table.

The second half is the important half: the 6h reverify sweep is the ONLY thing keeping the
connection cache true, and code does not land in the same instant as its migration. These
tests prove the sweep still writes when echo_social_connections.late_account_id does not
exist yet, and starts stamping when it does.
"""

import agent.portal_calendar_store as pcs
from agent import zernio_reverify as rv


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    @property
    def text(self):
        return "" if self._payload is None else str(self._payload)


class _Http:
    """PostgREST stand-in whose echo_social_connections has a CONFIGURABLE column set, so
    a test can run the same writer against a schema with and without late_account_id."""

    def __init__(self, cols, gyms=None):
        self.cols = set(cols)
        self.gyms = gyms or {"eng": "uuid-eng"}
        self.rows = {}
        self.posts = []

    def _table(self, url):
        return url.rstrip("/").split("/rest/v1/")[-1]

    def get(self, url, params=None, headers=None, timeout=None):
        t = self._table(url)
        params = params or {}
        if t == "gyms":
            slug = (params.get("slug") or "").replace("eq.", "")
            uuid = self.gyms.get(slug)
            return _Resp(200, [{"id": uuid}] if uuid else [])
        if t == "echo_social_connections":
            if "gym_id" in params:
                uuid = (params.get("gym_id") or "").replace("eq.", "")
                plat = (params.get("platform") or "").replace("eq.", "")
                row = self.rows.get((uuid, plat))
                return _Resp(200, [row] if row else [])
            return _Resp(200, list(self.rows.values()))
        return _Resp(200, [])

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        t = self._table(url)
        if t != "echo_social_connections":
            return _Resp(201, json or [])
        body = json if isinstance(json, dict) else (json or [{}])[0]
        self.posts.append(dict(body))
        unknown = set(body) - self.cols
        if unknown:
            col = sorted(unknown)[0]
            return _Resp(400, {"code": "42703",
                               "message": f'column "{col}" of relation '
                                          f'"echo_social_connections" does not exist'})
        row = dict(self.rows.get((body.get("gym_id"), body.get("platform"))) or {})
        row.update(body)
        self.rows[(body.get("gym_id"), body.get("platform"))] = row
        return _Resp(201, [row])


_NEW_COLS = {"id", "gym_id", "platform", "state", "handle", "first_connected_at",
             "last_verified_at", "updated_at", "late_account_id"}
_OLD_COLS = _NEW_COLS - {"late_account_id"}


def _store(http):
    pcs.SupabaseCalendarStore._late_account_id_column_absent = False
    return pcs.SupabaseCalendarStore(url="https://x.supabase.co", service_key="k", http=http)


def test_the_account_id_is_stamped_on_the_source_of_truth_row():
    http = _Http(_NEW_COLS)
    _store(http).rewrite_social_connection(
        "eng", "instagram", "connected", handle="eng", late_account_id="acct-ig-1")
    assert http.rows[("uuid-eng", "instagram")]["late_account_id"] == "acct-ig-1"


def test_the_write_still_lands_when_the_column_is_not_deployed_yet():
    """The live-outage guard. Before the migration, the sweep must still write state and
    handle: a stale connection cache is a client ticket per gym."""
    http = _Http(_OLD_COLS)
    out = _store(http).rewrite_social_connection(
        "eng", "instagram", "connected", handle="eng", late_account_id="acct-ig-1")
    assert out is not None
    row = http.rows[("uuid-eng", "instagram")]
    assert row["state"] == "connected" and row["handle"] == "eng"
    assert "late_account_id" not in row


def test_the_missing_column_is_remembered_so_the_fleet_pays_one_retry_not_one_per_gym():
    http = _Http(_OLD_COLS)
    store = _store(http)
    store.rewrite_social_connection("eng", "instagram", "connected",
                                    handle="eng", late_account_id="a1")
    first = len(http.posts)
    assert first == 2                      # one rejected, one retried without the column
    store.rewrite_social_connection("eng", "facebook", "connected",
                                    handle="eng", late_account_id="a2")
    assert len(http.posts) == first + 1     # second gym does not re-attempt
    pcs.SupabaseCalendarStore._late_account_id_column_absent = False


def test_a_non_schema_400_is_never_silently_downgraded_into_a_missing_column():
    """The retry must match the undefined-column error and NOTHING else, or a permissions
    failure or an outage would be quietly retried into partial data."""
    http = _Http(_NEW_COLS)

    def _post(url, params=None, headers=None, json=None, timeout=None):
        return _Resp(403, {"message": "permission denied for table "
                                      "echo_social_connections"})

    http.post = _post
    try:
        _store(http).rewrite_social_connection("eng", "instagram", "connected",
                                               handle="eng", late_account_id="a1")
    except pcs.PortalStoreError as exc:
        assert exc.status == 403
    else:
        raise AssertionError("a 403 must propagate, not be retried as a schema gap")


def test_column_missing_matcher_is_narrow():
    assert pcs._column_missing(
        _Resp(400, {"message": 'column "late_account_id" of relation '
                               '"echo_social_connections" does not exist'}),
        "late_account_id") is True
    assert pcs._column_missing(_Resp(400, {"code": "42703"}), "late_account_id") is True
    assert pcs._column_missing(
        _Resp(400, {"message": 'null value in column "handle" violates not-null'}),
        "late_account_id") is False
    assert pcs._column_missing(_Resp(500, None), "late_account_id") is False


# ---- the sweep passes the id through ----------------------------------------

class _FakeStore:
    def __init__(self):
        self.calls = []

    def rewrite_social_connection(self, base, platform, state, handle=None,
                                  mark_ever_connected=False, late_account_id=None):
        self.calls.append({"platform": platform, "state": state, "handle": handle,
                           "late_account_id": late_account_id})


class _FakeZernio:
    def list_accounts(self, pid):
        return {"accounts": [
            {"_id": "acct-ig", "platform": "instagram",
             "metadata": {"profileData": {"username": "eng"}}},
            {"_id": "acct-gbp", "platform": "googlebusiness",
             "metadata": {"locationName": "CrossFit ENG"}},
        ]}


def test_reverify_stamps_the_account_id_for_each_connected_platform(monkeypatch):
    monkeypatch.setattr(rv.config, "zernio_enabled", lambda: True)
    monkeypatch.setattr(rv._zr, "_resolve_profile_id",
                        lambda base, client=None, allow_find=False: "pid-1")
    store = _FakeStore()
    out = rv.reverify_gym("eng", client=_FakeZernio(), store=store, logger=lambda m: None)
    assert out["ok"] is True
    by = {c["platform"]: c for c in store.calls}
    assert by["instagram"]["late_account_id"] == "acct-ig"
    assert by["googlebusiness"]["late_account_id"] == "acct-gbp"


def test_reverify_stamps_nothing_for_a_platform_that_is_not_connected(monkeypatch):
    """A metric written against the wrong account is worse than a missing one."""
    monkeypatch.setattr(rv.config, "zernio_enabled", lambda: True)
    monkeypatch.setattr(rv._zr, "_resolve_profile_id",
                        lambda base, client=None, allow_find=False: "pid-1")
    store = _FakeStore()
    rv.reverify_gym("eng", client=_FakeZernio(), store=store, logger=lambda m: None)
    by = {c["platform"]: c for c in store.calls}
    assert by["facebook"]["state"] == "not_connected"
    assert by["facebook"]["late_account_id"] is None
