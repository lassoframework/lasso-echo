"""Offline safety checks for bounded LASSO held-feed repair."""

from copy import deepcopy
from datetime import date

from agent import variant_regen
from agent.jobs import lasso_held_media_repair as repair


def _row(rid, day="2026-10-05", **overrides):
    row = {key: None for key in repair._CAS_COLUMNS}
    row.update(id=rid, gym_id="lasso", status="pending", variant_status="active",
               account="instagram", format="feed", post_date=day,
               caption=f"Approved copy for {rid}.", image_url=f"https://old.example/{rid}.jpg",
               media_not_ready_reason=repair.HOLD_REASON,
               created_at="2026-10-01T00:00:00+00:00", slot_index=0)
    row.update(overrides)
    return row


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Store:
    def __init__(self, rows):
        self.rows = {row["id"]: deepcopy(row) for row in rows}
        self.cache = {}
        self.patches = []
        self.reads = 0

    def list_pending_media_between(self, gym, first, last):
        self.reads += 1
        assert (gym, first, last) == ("lasso", "2026-10-05", "2026-10-06")
        return [deepcopy(row) for row in self.rows.values()]

    def get_row(self, gym, rid):
        assert gym == "lasso"
        return deepcopy(self.rows[rid])

    def _client(self):
        return self

    @staticmethod
    def _rest(name):
        return name

    @staticmethod
    def _headers(extra=None):
        return extra or {}

    def get(self, url, params, headers, timeout):
        assert url == "echo_infographic_artifacts"
        key = (params["source_identity->>source_id"],
               params["source_identity->>source_hash"])
        return _Response(self.cache.get(key, []))

    def patch(self, url, params, headers, json, timeout):
        assert url == "content_calendar"
        self.patches.append((params, json))
        rid = params["id"].removeprefix('eq."').removesuffix('"')
        row = self.rows[rid]
        # Simulate the database's full-field CAS, including a racing caption edit.
        if any(params[key] != repair._eq(row[key]) for key in repair._CAS_COLUMNS):
            return _Response([])
        row.update(json)
        return _Response([deepcopy(row)])


class _Artifacts:
    available = True

    def __init__(self):
        self.claims = []
        self.releases = []

    def claim(self, tenant, key, owner):
        self.claims.append((tenant, key, owner))
        return True

    def release(self, tenant, key, owner):
        self.releases.append((tenant, key, owner))


def _armed(monkeypatch):
    monkeypatch.setattr(repair.config, "lasso_three_feed_enabled", lambda: True)
    monkeypatch.setattr(repair.config, "lasso_infographic_quality_enabled",
                        lambda key: key == "lasso_ig")
    monkeypatch.setattr(variant_regen, "enabled", lambda: True)
    monkeypatch.setattr(repair.visual_writer_prepare, "enabled", lambda: False)
    monkeypatch.setattr(repair, "_local_day", lambda now: date(2026, 10, 5))


def test_repair_is_lasso_ig_feed_only_and_capped_per_day(monkeypatch):
    _armed(monkeypatch)
    rows = ([_row(f"today-{i}") for i in range(4)]
            + [_row(f"tomorrow-{i}", day="2026-10-06") for i in range(4)]
            + [_row("client", gym_id="client-gym")]
            + [_row("story", format="story")]
            + [_row("approved", status="approved")])
    store, artifacts = _Store(rows), _Artifacts()
    generated = []

    def fake_generate(row, account, **kwargs):
        generated.append((row["id"], account))
        return {"ok": True, "image_url": f"https://new.example/{row['id']}.png"}

    monkeypatch.setattr(variant_regen, "generate_variant_image", fake_generate)
    out = repair.run(store=store, artifact_store=artifacts)

    assert out["ok"] is True
    assert out["attempted"] == out["generated"] == out["repaired"] == 6
    assert len(generated) == len(artifacts.claims) == len(artifacts.releases) == 6
    assert all(account == "lasso_ig" for _, account in generated)
    assert store.rows["today-0"]["caption"] == "Approved copy for today-0."
    assert store.rows["today-0"]["source_media_url"] == "https://new.example/today-0.png"
    assert store.rows["today-0"]["media_not_ready_reason"] is None
    assert store.rows["today-3"]["media_not_ready_reason"] == repair.HOLD_REASON
    assert store.rows["tomorrow-3"]["media_not_ready_reason"] == repair.HOLD_REASON
    assert store.rows["client"]["media_not_ready_reason"] == repair.HOLD_REASON


def test_changed_row_after_generation_is_not_swapped_and_cached_artifact_reuses(
        monkeypatch):
    _armed(monkeypatch)
    store, artifacts = _Store([_row("one")]), _Artifacts()
    generated = []

    def fake_generate(row, account, **kwargs):
        generated.append(row["id"])
        # Simulate a concurrent media edit after the paid generation. The real
        # variant path has persisted the reviewed artifact by this point.
        store.rows["one"]["image_url"] = "https://old.example/edited.jpg"
        source_id = f"content_calendar:{row['id']}:caption"
        source_hash = repair.hashlib.sha256(row["caption"].encode()).hexdigest()
        store.cache[(repair._eq(source_id), repair._eq(source_hash))] = [{
            "image_url": "https://new.example/one.png",
            "source_identity": {"source_id": source_id, "source_hash": source_hash},
            "evidence": {"policy_version": repair.infographic_evidence.POLICY_VERSION,
                         "brain_snapshot": {"source": "hash"},
                         "brief_model": "gpt-6-astra", "grade_status": "PASS",
                         "image_sha256": "hash", "review_response_id": "review"},
        }]
        return {"ok": True, "image_url": "https://new.example/one.png"}

    monkeypatch.setattr(variant_regen, "generate_variant_image", fake_generate)
    monkeypatch.setattr(repair.infographic_evidence, "brain_snapshot",
                        lambda: {"source": "hash"})
    first = repair.run(store=store, artifact_store=artifacts)
    assert first["generated"] == 1 and first["repaired"] == 0
    assert store.patches == []

    second = repair.run(store=store, artifact_store=artifacts)
    assert second["generated"] == 0 and second["reused"] == 1
    assert second["repaired"] == 1
    assert generated == ["one"]
    params, payload = store.patches[0]
    assert params["caption"] == repair._eq("Approved copy for one.")
    assert params["status"] == repair._eq("pending")
    assert params["media_not_ready_reason"] == repair._eq(repair.HOLD_REASON)
    assert payload["media_not_ready_reason"] is None


def test_disarmed_repair_never_reads_or_generates(monkeypatch):
    monkeypatch.setattr(repair.config, "lasso_three_feed_enabled", lambda: False)
    store = _Store([_row("one")])
    out = repair.run(store=store, artifact_store=_Artifacts())
    assert out["ok"] is False
    assert store.reads == 0
    assert store.patches == []


def test_daily_runner_repairs_ahead_of_drafting_even_if_voice_is_missing(monkeypatch):
    from agent import runner
    monkeypatch.setattr(runner.config, "master_enabled", lambda: True)
    monkeypatch.setattr(runner.config, "lasso_three_feed_enabled", lambda: True)
    monkeypatch.setattr(runner, "load_voice", lambda path: None)
    calls = []
    monkeypatch.setattr(repair, "run", lambda **kwargs:
                        calls.append(kwargs) or {"ok": True, "attempted": 0,
                        "generated": 0, "reused": 0, "repaired": 0,
                        "skipped": 0, "errors": 0})

    class Poster:
        def post_notice(self, message):
            pass

    out = runner.run_daily(poster=Poster(), scheduled_for="2026-10-05T12:00:00+00:00")
    assert out["status"] == "no_voice"
    assert calls == [{"now": "2026-10-05T12:00:00+00:00"}]
