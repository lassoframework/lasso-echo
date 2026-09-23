import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "stage_lasso_daily_summit.py"
SPEC = importlib.util.spec_from_file_location("stage_lasso_daily_summit", SCRIPT)
stage = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(stage)


class Artifacts:
    class Response:
        status_code = 200
        def json(self):
            return [{"tenant": "lasso", "image_url": "https://reviewed.example/summit.png",
                     "image_sha256": "b" * 64,
                     "evidence": {"grade_status": "PASS", "image_sha256": "b" * 64, "policy_version": "p1"},
                     "source_identity": self.source}]
    class Http:
        def __init__(self, owner): self.owner = owner
        def get(self, *args, **kwargs):
            response = Artifacts.Response(); response.source = self.owner.source; return response
    def __init__(self):
        self.url = "https://supabase.test/rest/v1/echo_infographic_artifacts"
        self.headers = {"Authorization": "Bearer test"}
        self.http = self.Http(self)
    def cached(self, tenant, key):
        self.args = tenant, key
        _, day, digest = key.split(":", 2)
        self.source = {"source_id": "lasso_summit_daily_catalog:" + day,
                       "source_hash": digest}
        return "https://reviewed.example/summit.png"


def catalog(tmp_path):
    posts = []
    for day in ("2026-09-23", "2026-09-24"):
        posts.append({"date": day, "caption": "Exact " + day, "on_image": {"headline": "H", "facts": ["F"], "cta": "C", "footer": "L"}})
    # The implementation range is deliberately fixed.  This fixture tests the
    # public action helpers directly, so bypass the complete-range catalog check.
    return posts


def test_deterministic_id_is_account_day_campaign_scoped():
    assert stage.deterministic_id("instagram", "2026-09-23") == stage.deterministic_id("instagram", "2026-09-23")
    assert stage.deterministic_id("instagram", "2026-09-23") != stage.deterministic_id("facebook", "2026-09-23")


def test_build_plan_preserves_original_and_uses_exact_cached_url(monkeypatch, tmp_path):
    cat = tmp_path / "catalog.json"
    posts = []
    from datetime import date, timedelta
    day = date(2026, 9, 23)
    while day <= date(2026, 11, 8):
        posts.append({"date": day.isoformat(), "caption": "Exact " + day.isoformat(), "visual_concept": "paper", "hosted_image_url": "https://reviewed.example/summit.png", "on_image": {"headline": "H", "facts": ["F"], "cta": "C", "footer": "L"}}); day += timedelta(days=1)
    cat.write_text(json.dumps({"posts": posts, "visual_concepts": [{"id": "paper", "direction": "paper planning pages"}]}))
    original = {"id": "old", "gym_id": "lasso", "account": "instagram", "format": "feed", "post_date": "2026-09-23", "scheduled_at": "2026-09-23T09:00:00-04:00", "status": "pending", "variant_status": "active", "slot_index": 0, "caption": "Nashville original", "image_url": "https://supplied/original.png", "pillar": "summit", "late_post_id": None, "published_at": None}
    upstream = {"mutations": [{"reason": "move_existing_summit_original_to_additive_slot", "row_id": "old", "date": "2026-09-23", "account": "instagram", "set": {"scheduled_at": "2026-09-23T12:00:00-04:00"}}], "inserts": [{"row": {"gym_id": "lasso", "format": "feed", "slot_index": 2, "pillar": "summit", "account": "facebook", "post_date": "2026-09-23", "scheduled_at": "2026-09-23T12:00:00-04:00"}}]}
    plan = stage.build_plan({"rows": [original]}, upstream, cat, artifact_store=Artifacts())
    move, insert = plan["actions"]
    assert move["expected"]["caption"] == "Nashville original"
    assert move["expected"]["image_url"] == "https://supplied/original.png"
    assert move["set"]["slot_index"] == 2
    assert insert["row"]["caption"] == "Exact 2026-09-23"
    assert insert["row"]["image_url"] == "https://reviewed.example/summit.png"
    assert insert["row"]["id"] == stage.deterministic_id("facebook", "2026-09-23")


def test_plan_blocks_insert_without_reviewed_artifact(tmp_path):
    # Use the repository catalog, but no artifact cache: planning records a block
    # instead of creating an unreviewed URL or doing any write.
    schema = {"id": "one", "gym_id": "lasso", "account": "instagram", "format": "feed", "post_date": "2026-09-23", "scheduled_at": None, "slot_index": 0, "status": "pending", "variant_status": "active", "pillar": "book", "caption": "x", "image_url": "u"}
    out = stage.build_plan({"rows": [schema]}, {"mutations": [], "inserts": [{"row": {"gym_id": "lasso", "format": "feed", "slot_index": 2, "pillar": "summit", "account": "instagram", "post_date": "2026-09-23"}}]}, stage.DEFAULT_CATALOG, artifact_store=None)
    assert out["actions"] == []
    assert out["blocked"][0]["reason"] == "reviewed_artifact_missing"


def test_apply_zero_row_move_is_conflict_and_never_retried(tmp_path):
    plan = {"actions": [{"operation": "guarded_move", "date": "2026-09-23", "account": "instagram", "expected": {"id": "x", "gym_id": "lasso", "account": "instagram", "post_date": "2026-09-23", "format": "feed", "status": "pending", "variant_status": "active"}, "set": {"slot_index": 2}}]}
    calls = []
    receipts = stage.apply(plan, object(), tmp_path, current_day_fn=lambda *_: [], move_fn=lambda *_: calls.append(1) or None)
    assert calls == [1]
    assert receipts[0]["status"] == "conflict"
    assert (tmp_path / "calendar-before-apply.json").exists()
    assert (tmp_path / "summit-stage-receipts.json").exists()


def test_apply_readback_requires_one_ordinal_two_summit(tmp_path):
    plan = {"actions": [_insert_action()]}
    reads = [[], [{"account": "instagram", "caption": "Summit", "slot_index": 0}]]
    receipts = stage.apply(plan, object(), tmp_path, current_day_fn=lambda *_: reads.pop(0), insert_fn=lambda *_: {"result": "inserted"})
    assert receipts[0]["status"] == "readback_conflict"


def _insert_action():
    return {"operation": "insert_if_primary_key_missing", "date": "2026-09-23", "account": "instagram",
            "row": {"id": "new", "gym_id": "lasso", "account": "instagram", "post_date": "2026-09-23", "pillar": "summit", "format": "feed", "caption": "Summit", "image_url": "https://reviewed.example/summit.png", "slot_index": 2, "status": "pending", "variant_status": "active"},
            "artifact_tenant": "lasso", "source_identity": {"source_id": "s", "source_hash": "a" * 64},
            "policy_version": "p1", "image_sha256": "b" * 64}


def test_apply_surfaces_atomic_conflict_without_direct_insert(tmp_path):
    plan = {"actions": [_insert_action()]}
    calls = []
    rows = [{"id": "old", "account": "instagram", "caption": "Nashville Summit", "slot_index": 0}]
    receipts = stage.apply(plan, object(), tmp_path, current_day_fn=lambda *_: rows,
                           insert_fn=lambda *_: calls.append(1) or {"result": "conflict", "reason": "occupied_logical_slot"})
    assert receipts[0]["status"] == "conflict"
    assert receipts[0]["reason"] == "occupied_logical_slot"
    assert calls == [1]


def test_apply_routes_existing_id_through_rpc_idempotency(tmp_path):
    plan = {"actions": [_insert_action()]}
    rows = [{"id": "new", "account": "instagram", "caption": "Summit",
             "slot_index": 2, "status": "pending"}]
    calls = []
    receipts = stage.apply(plan, object(), tmp_path, current_day_fn=lambda *_: rows,
                           insert_fn=lambda *_: calls.append(1) or {"result": "idempotent", "id": "new"})
    assert calls == [1]
    assert receipts[0]["status"] == "already_present"


def test_apply_rejects_foreign_insert_before_read_or_write(tmp_path):
    plan = {"actions": [{"operation": "insert_if_primary_key_missing", "date": "2026-09-23", "account": "instagram", "row": {"id": "new", "gym_id": "other", "account": "instagram", "post_date": "2026-09-23", "format": "feed", "slot_index": 2, "status": "pending", "variant_status": "active"}}]}
    calls = []
    receipts = stage.apply(plan, object(), tmp_path, current_day_fn=lambda *_: calls.append("read") or [], insert_fn=lambda *_: calls.append("write"))
    assert receipts[0]["status"] == "out_of_scope_input"
    assert calls == []


def test_atomic_insert_posts_only_to_rpc_with_provenance():
    class Response:
        status_code = 200
        def json(self): return {"result": "inserted", "id": "new"}
    class Client:
        def post(self, *args, **kwargs): self.args = args; self.kwargs = kwargs; return Response()
    class Store:
        def __init__(self): self.client = Client()
        def _client(self): return self.client
        def _rest(self, name): return "https://supabase.test/rest/v1/" + name
        def _headers(self, extra): return extra
    store = Store()
    result = stage._atomic_insert(store, _insert_action())
    assert result["result"] == "inserted"
    assert store.client.args[0].endswith("/rpc/stage_lasso_campaign_row")
    assert store.client.kwargs["json"]["p_source_hash"] == "a" * 64
    assert "/content_calendar" not in store.client.args[0]


def test_denied_summit_does_not_block_current_occupancy(tmp_path):
    rows = [{"id": "old", "account": "instagram", "caption": "Nashville Summit", "slot_index": 0, "status": "denied"}, {"id": "new", "account": "instagram", "caption": "Nashville Summit", "slot_index": 2, "status": "pending"}]
    stage._assert_one_summit(rows, "instagram")


def test_cas_uses_is_null_for_null_slot():
    class Response:
        status_code = 200
        def json(self): return []
    class Client:
        def patch(self, *args, **kwargs): self.kwargs = kwargs; return Response()
    class Store:
        def __init__(self): self.client = Client()
        def _client(self): return self.client
        def _rest(self, _): return "url"
        def _headers(self, extra): return extra
    store = Store()
    stage._cas_move(store, {"id": "i", "post_date": "2026-09-23", "account": "instagram", "status": "pending", "variant_status": "active", "slot_index": None, "image_url": "u", "caption": "c"}, {"slot_index": 2})
    assert store.client.kwargs["params"]["slot_index"] == "is.null"


def test_apply_move_refuses_other_live_summit_before_write(tmp_path):
    plan = {"actions": [{"operation": "guarded_move", "date": "2026-09-23", "account": "instagram", "expected": {"id": "move", "gym_id": "lasso", "post_date": "2026-09-23", "account": "instagram", "format": "feed", "status": "pending", "variant_status": "active"}, "set": {"slot_index": 2}}]}
    calls = []
    rows = [{"id": "other", "account": "instagram", "caption": "Nashville Summit", "slot_index": 2, "status": "pending"}]
    receipts = stage.apply(plan, object(), tmp_path, current_day_fn=lambda *_: rows, move_fn=lambda *_: calls.append(1))
    assert receipts[0]["status"] == "conflict_existing_summit_or_slot"
    assert calls == []


def test_campaign_shape_audit_requires_two_regular_and_one_summit_on_each_platform():
    def read(_store, day):
        rows = []
        for account in ("instagram", "facebook"):
            for slot, pillar in ((0, "book"), (1, "platform"), (2, "summit")):
                if day == "2026-09-24" and account == "facebook" and slot == 1:
                    continue
                rows.append({"id": f"{day}:{account}:{slot}", "account": account,
                             "pillar": pillar, "slot_index": slot, "status": "pending",
                             "variant_status": "active"})
        return rows

    report = stage.campaign_shape_report(object(), current_day_fn=read)
    assert report["total"] == 94
    assert report["complete"] == 93
    gap = next(item for item in report["days"] if item["date"] == "2026-09-24"
               and item["account"] == "facebook")
    assert gap["status"] == "partial"
    assert gap["regular_slots"] == [0]
    assert gap["summit_slots"] == [2]


def test_campaign_shape_audit_rejects_third_regular_and_misplaced_summit():
    def read(_store, _day):
        return [{"id": str(slot), "account": "instagram", "pillar": pillar,
                 "slot_index": slot, "status": "pending", "variant_status": "active"}
                for slot, pillar in ((0, "book"), (1, "website"), (2, "book"))]

    report = stage.campaign_shape_report(object(), current_day_fn=read)
    assert report["complete"] == 0
    assert report["days"][0]["regular_slots"] == [0, 1, 2]
