"""Offline guards for the exact, default-off Nine7 media permutation."""
import copy
import json
import stat

import pytest

from scripts import reflow_nine7_burst_media as op
from agent.portal_calendar_store import SupabaseCalendarStore
from agent import visual_writer_prepare

KEY = "1fba4eac7339d4c882b8392b7ca3f955d991b27b96cbd5af51bd596a7c8b8cae"


def row(day, kind, source, *, status="pending"):
    name = source if kind == "ig" else f"{kind}-{day}"
    return {"id": op.TARGET_ROOTS.get(day, name) if kind == "ig" else name,
            "gym_id": op.GYM, "post_date": day, "status": status,
            "variant_status": "active", "account": {
                "ig": "instagram", "fb": "facebook", "story": "instagram",
                "gbp": "googlebusiness"}[kind],
            "format": "story" if kind == "story" else "feed",
            "caption": f"exact caption {kind} {day}",
            "image_url": f"https://cdn/{source}", "source_media_url": f"https://cdn/{source}",
            "source_media_asset_id": None, "thumbnail_url": None,
            "media_not_ready_reason": None, "created_at": "2026-10-01T00:00:00Z",
            "scheduled_at": f"{day}T15:00:00Z", "slot_index": 0,
            "published_at": None, "late_post_id": None, "publish_claim_token": None,
            "publish_reservation_day": None}


class Calendar:
    def __init__(self, rows):
        self.rows = {r["id"]: copy.deepcopy(r) for r in rows}
        self.writes = []
        self.conflict = None
        self.drift = None
    def rows_in_range_complete(self, gym, first, last, *, all_statuses):
        assert gym == op.GYM and all_statuses
        return [copy.deepcopy(r) for r in self.rows.values()
                if first <= r["post_date"] <= last]
    def list_photo_restage_book(self, gym):
        assert gym == op.GYM
        return [copy.deepcopy(r) for r in self.rows.values()]
    def get_row(self, gym, rid):
        assert gym == op.GYM
        value = copy.deepcopy(self.rows.get(rid))
        if value and self.drift == rid:
            value["caption"] = "concurrent edit"
        return value
    def reflow_pending_burst_media(self, gym, current, **payload):
        self.writes.append(current["id"])
        if current["id"] == self.conflict:
            return None
        self.rows[current["id"]].update({k: v for k, v in payload.items()
                                         if k not in ("render_evidence", "first", "last",
                                                      "ticket", "request_key")})
        return copy.deepcopy(self.rows[current["id"]])


def fixture(monkeypatch, tmp_path, *, alt=("A", "B", "C")):
    days = [f"2026-10-{d:02d}" for d in range(20, 29)]
    names = ["IMG_2600.jpg"] + [f"IMG_{n}.jpg" for n in (2715, 2717, 2719, 2720, 2728, 2731)]
    names += [f"IMG_{n}.jpg" for n in (2800, 2900)]
    sources = dict(zip(days, names))
    if not alt:
        sources[days[0]] = "IMG_2714.jpg"
        sources[days[-2]] = "IMG_2729.jpg"
        sources[days[-1]] = "IMG_2730.jpg"
    rows = [row(day, kind, source) for day, source in sources.items()
            for kind in ("ig", "fb", "story", "gbp")]
    calendar = Calendar(rows)
    photos = {source: {"path": str(tmp_path / source), "sha256": "a" * 64,
                       "cohort": ("burst-27" if source[4:6] == "27" else
                                  "burst-26" if source[4:6] == "26" else
                                  f"burst-{source[4:6]}")}
              for source in sources.values()}
    monkeypatch.setattr(op, "_library", lambda _lib: photos)
    args = dict(store=calendar, gym=op.GYM, first=days[0], last=days[-1],
                ticket=op.TICKET, request_key=KEY, library_path=str(tmp_path),
                today="2026-10-06")
    return calendar, args


def prepared(_gym, root, siblings, photo):
    source = photo["path"].rsplit("/", 1)[-1]
    def variant(r):
        return {"ok": True, "kind": "photo", "key": source,
                "image_url": f"https://new/{source}/{r['id']}",
                "source_media_url": f"https://new/{source}",
                "source_media_asset_id": None}
    return {**variant(root), "siblings": {r["id"]: variant(r) for r in siblings}}


def test_exact_nine7_sequence_improves_only_by_pure_permutation(monkeypatch, tmp_path):
    calendar, args = fixture(monkeypatch, tmp_path)
    before = copy.deepcopy(calendar.rows)
    plan = op.run(**args)["preflight"]
    assert plan["baseline"][0] == 5 and plan["after"][0] < 5
    assert plan["after"][1] <= plan["baseline"][1]
    assert sorted(m["from"] for m in plan["moves"]) == sorted(m["to"] for m in plan["moves"])
    assert calendar.rows == before and not calendar.writes


def test_thin_one_cohort_library_refuses(monkeypatch, tmp_path):
    _, args = fixture(monkeypatch, tmp_path, alt=())
    with pytest.raises(ValueError, match="thin library"):
        op.plan(**{k: v for k, v in args.items() if k != "store"}, store=args["store"])


def test_protected_target_and_human_exchange_cannot_move(monkeypatch, tmp_path):
    calendar, args = fixture(monkeypatch, tmp_path)
    calendar.rows[op.TARGET_ROOTS["2026-10-23"]]["status"] = "approved"
    with pytest.raises(ValueError, match="target root|protected"):
        op.run(**args)
    calendar, args = fixture(monkeypatch, tmp_path)
    for r in calendar.rows.values():
        if r["post_date"] in ("2026-10-20", "2026-10-27", "2026-10-28"):
            r["status"] = "approved"
    with pytest.raises(ValueError, match="thin library"):
        op.run(**args)


def test_missing_coupled_sibling_refuses(monkeypatch, tmp_path):
    calendar, args = fixture(monkeypatch, tmp_path)
    calendar.rows.pop("gbp-2026-10-23")
    with pytest.raises(ValueError, match="coupled platform"):
        op.run(**args)


def test_scope_ticket_and_short_window_refused(monkeypatch, tmp_path):
    _, args = fixture(monkeypatch, tmp_path)
    for changed in ({"gym": "foreign"}, {"ticket": "wrong"},
                    {"first": "2026-10-22"}, {"last": "2026-10-25"},
                    {"first": "2026-09-01"}):
        with pytest.raises(ValueError):
            op.run(**{**args, **changed})


def test_out_of_window_use_of_a_source_refuses(monkeypatch, tmp_path):
    calendar, args = fixture(monkeypatch, tmp_path)
    historical = row("2026-10-19", "ig", "IMG_2800.jpg", status="published")
    calendar.rows[historical["id"]] = historical
    with pytest.raises(ValueError, match="out-of-window"):
        op.run(**args)


def test_apply_preserves_copy_dates_status_and_coupled_rows(monkeypatch, tmp_path):
    calendar, args = fixture(monkeypatch, tmp_path)
    before = copy.deepcopy(calendar.rows)
    digest = op.run(**args)["preflight"]["target_digest"]
    receipt = tmp_path / "receipt.json"
    monkeypatch.setenv("ECHO_NINE7_BURST_REFLOW_ENABLED", "true")
    result = op.run(**args, apply=True, expected_digest=digest, receipt_path=receipt,
                    prepare_fn=prepared)
    assert result["ok"] and calendar.writes
    assert json.loads(receipt.read_text())["state"] == "readback_verified"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    for rid, after in calendar.rows.items():
        assert all(after[k] == before[rid][k] for k in before[rid] if k not in op._MEDIA)
    for day in {r["post_date"] for r in calendar.rows.values()}:
        assert len({r["source_media_url"] for r in calendar.rows.values()
                    if r["post_date"] == day}) == 1
    again = op.run(**args, apply=True, expected_digest=digest, receipt_path=receipt,
                   prepare_fn=prepared)
    assert again["replay"] and len(calendar.writes) == len(result["changed_ids"])


def test_partial_render_or_write_and_concurrent_drift_are_receipted(monkeypatch, tmp_path):
    for mode in ("render", "write", "drift"):
        calendar, args = fixture(monkeypatch, tmp_path)
        planned = op.run(**args)["preflight"]
        digest = planned["target_digest"]
        first_moved = planned["moves"][0]["rows"][0]["id"]
        receipt = tmp_path / f"{mode}.json"
        monkeypatch.setenv("ECHO_NINE7_BURST_REFLOW_ENABLED", "true")
        if mode == "write":
            calendar.conflict = first_moved
        if mode == "drift":
            calendar.drift = first_moved
        def broken(gym, root, siblings, photo):
            value = prepared(gym, root, siblings, photo)
            if mode == "render":
                value["siblings"].pop(siblings[0]["id"])
            return value
        result = op.run(**args, apply=True, expected_digest=digest,
                        receipt_path=receipt, prepare_fn=broken)
        assert not result["ok"]
        assert json.loads(receipt.read_text())["state"] == "reconcile_required"
        assert not op.run(**args, apply=True, expected_digest=digest,
                          receipt_path=receipt, prepare_fn=prepared)["ok"]


def test_store_write_is_exact_tenant_status_media_cas(monkeypatch):
    monkeypatch.setattr(visual_writer_prepare, "enabled", lambda: False)
    current = row("2026-10-21", "ig", "IMG_2715.jpg")
    current["time_slot"] = "afternoon"
    class Response:
        status_code = 200
        def __init__(self, rows):
            self.rows = rows
        def json(self):
            return self.rows
    class HTTP:
        def __init__(self):
            self.calls = []
            self.row = copy.deepcopy(current)
        def patch(self, _url, *, params, json, headers, timeout):
            self.calls.append((params, json))
            for key in ("gym_id", "id", "status", "caption", "post_date", "image_url",
                        "source_media_url", "publish_claim_token", "time_slot"):
                assert key in params
                expected = "is.null" if self.row.get(key) is None else f"eq.{self.row[key]}"
                if params[key] != expected:
                    return Response([])
            self.row.update(json)
            return Response([copy.deepcopy(self.row)])
    http = HTTP()
    store = SupabaseCalendarStore(url="https://example.supabase.co", service_key="test", http=http)
    kwargs = dict(first="2026-10-20", last="2026-10-28", ticket=op.TICKET,
                  request_key=KEY, image_url="https://new/other.jpg",
                  source_media_url="https://new/raw.jpg", source_media_asset_id=None)
    assert store.reflow_pending_burst_media(op.GYM, current, **kwargs)["image_url"] == kwargs["image_url"]
    assert len(http.calls) == 1
    assert store.reflow_pending_burst_media("foreign", current, **kwargs) is None
    assert store.reflow_pending_burst_media(op.GYM, {**current, "status": "approved"}, **kwargs) is None
    assert store.reflow_pending_burst_media(op.GYM, {**current, "publish_claim_token": "claim"}, **kwargs) is None
    assert len(http.calls) == 1
