import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "refresh_lasso_calendar.py"
SPEC = importlib.util.spec_from_file_location("refresh_lasso_calendar", SCRIPT)
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)


def row(id, day, *, status="pending", image="https://cdn/card.png", caption="regular"):
    return {"id": id, "gym_id": "lasso", "post_date": day, "status": status,
            "account": "instagram", "format": "feed", "image_url": image,
            "caption": caption, "variant_status": "active"}


def test_plan_needs_summit_and_groups_shared_pending_image(tmp_path):
    rows = [row("a", "2026-09-24", image="https://cdn/same.png"),
            row("b", "2026-09-24", image="https://cdn/same.png"),
            row("fb", "2026-09-24", image="https://cdn/same.png") | {"account": "facebook"},
            row("candidate", "2026-09-24", caption="Nashville Growth Summit") | {"variant_status": "candidate"},
            row("denied", "2026-09-24", status="denied", caption="Nashville Growth Summit")]
    out = refresh.plan(rows, start="2026-09-24", end="2026-09-24", root=tmp_path)
    day = out["days"][0]
    assert day["active_feed_count"] == 2  # candidate and denied history are excluded
    assert day["summit_active_count"] == 0
    assert day["summit_action"] == "needs_new_summit_pending_post"
    assert out["regeneration_groups"][0]["anchor_row_ids"] == ["a", "b", "fb"]
    assert "denied" not in out["regeneration_groups"][0]["anchor_row_ids"]


def test_plan_skips_videos_and_book_originals(tmp_path):
    (tmp_path / "book_manifest.json").write_text('{"book.png": "https://cdn/book.png"}')
    rows = [row("video", "2026-09-24", image="https://cdn/movie.mp4"),
            row("book", "2026-09-24", image="https://cdn/book.png")]
    out = refresh.plan(rows, start="2026-09-24", end="2026-09-24", root=tmp_path)
    assert out["regeneration_groups"] == []


def test_plan_uses_exact_recorded_library_copy(tmp_path):
    rows = [row("old", "2026-09-24", image="https://cdn/old.png")]
    evidence = {"old.png": {"image_sha256": "abc", "grade_status": "PASS",
                "infographic_copy": {"headline": "Approved headline", "facts": ["Approved fact"],
                                      "cta": "Approved CTA", "footer": "LASSO"}}}
    out = refresh.plan(rows, start="2026-09-24", end="2026-09-24", root=tmp_path,
                       copy_evidence=evidence)
    source = out["regeneration_groups"][0]["evidence"][0]
    assert source["kind"] == "library_copy"
    assert source["infographic_copy"]["headline"] == "Approved headline"


def test_approved_copy_uses_renderer_display_normalization(tmp_path):
    evidence = {"card.png": {"infographic_copy": {"headline": "A: B", "facts": ["One; two"],
                "cta": "Do: it", "footer": "HTTPS://lassoframework.com"}}}
    manifest = refresh.plan([row("a", "2026-09-24")], start="2026-09-24", end="2026-09-24", root=tmp_path,
                            copy_evidence=evidence)
    copy = manifest["regeneration_groups"][0]["generation"]["approved_copy"]
    assert copy == {"headline": "A B", "facts": ["One, two"], "cta": "Do it", "footer": "lassoframework.com"}


def test_apply_requires_passing_explicit_review(tmp_path):
    manifest = refresh.plan([row("a", "2026-09-24")], start="2026-09-24", end="2026-09-24", root=tmp_path)
    class Store:
        def get_row(self, *_):
            return row("a", "2026-09-24")
    try:
        refresh.apply_reviewed_swaps(manifest, {"reviewed_groups": []}, Store(), tmp_path)
    except ValueError as exc:
        assert "passing" in str(exc)
    else:
        raise AssertionError("apply requires an external passing review")


def test_apply_uses_guarded_rows_and_readback(tmp_path):
    image = tmp_path / "reviewed.png"
    image.write_bytes(b"reviewed pixels")
    evidence = {"card.png": {"infographic_copy": {"headline": "Approved", "facts": ["Fact"]}}}
    manifest = refresh.plan([row("a", "2026-09-24")], start="2026-09-24", end="2026-09-24", root=tmp_path,
                            copy_evidence=evidence)
    group = manifest["regeneration_groups"][0]
    state = row("a", "2026-09-24")
    class Store:
        def get_row(self, *_):
            return dict(state)
    def guarded_swap(_store, expected, image_url):
        assert expected["image_url"] == state["image_url"]
        state["image_url"] = image_url
        return dict(state)
    review = {"reviewed_groups": [{"id": group["id"], "review_status": "PASS",
              "image_sha256": __import__("hashlib").sha256(image.read_bytes()).hexdigest(), "path": str(image)}]}
    class ArtifactStore:
        def save(self, *args):
            assert args[3]["source_id"] == "content_calendar:a"
    receipts = refresh.apply_reviewed_swaps(manifest, review, Store(), tmp_path, swap_fn=guarded_swap,
                                             host_fn=lambda *_: "https://cdn/reviewed.png",
                                             artifact_store=ArtifactStore(),
                                             review_fn=lambda *_: {"grade_status": "PASS", "image_sha256": review["reviewed_groups"][0]["image_sha256"],
                                                                   "infographic_copy": group["generation"]["approved_copy"]})
    assert receipts == [{"id": "a", "group_id": group["id"], "status": "applied"}]
    assert (tmp_path / "before-apply.json").exists()
    assert (tmp_path / "apply-receipts.json").exists()


def test_local_generation_uses_recorded_copy_and_resumes(tmp_path):
    rows = [row("a", "2026-09-24", image="https://cdn/old.png")]
    evidence = {"old.png": {"infographic_copy": {"headline": "Approved", "facts": ["Fact"],
                "cta": "CTA", "footer": "LASSO"}}}
    manifest = refresh.plan(rows, start="2026-09-24", end="2026-09-24", root=tmp_path,
                            copy_evidence=evidence)
    calls = []
    def renderer(headline, facts, **kwargs):
        calls.append((headline, facts, kwargs["cta"]))
        Path(kwargs["out_path"]).write_bytes(b"pixels")
        return {"path": kwargs["out_path"]}
    result = refresh.generate_review_assets(manifest, tmp_path / "generated", generate_fn=renderer)
    assert result[0]["status"] == "generated_local_review_required"
    assert calls == [("Approved", ["Fact"], "CTA")]
    assert refresh.generate_review_assets(manifest, tmp_path / "generated", generate_fn=renderer) == result
    assert len(calls) == 1


def test_generation_handles_missing_renderer_path(tmp_path):
    evidence = {"card.png": {"infographic_copy": {"headline": "Approved", "facts": ["Fact"]}}}
    manifest = refresh.plan([row("a", "2026-09-24")], start="2026-09-24", end="2026-09-24", root=tmp_path,
                            copy_evidence=evidence)
    result = refresh.generate_review_assets(manifest, tmp_path / "generated", generate_fn=lambda *_a, **_k: None)
    assert result[0]["reason"] == "render_failed"


def test_generation_catches_renderer_exception(tmp_path):
    evidence = {"card.png": {"infographic_copy": {"headline": "Approved", "facts": ["Fact"]}}}
    manifest = refresh.plan([row("a", "2026-09-24")], start="2026-09-24", end="2026-09-24", root=tmp_path,
                            copy_evidence=evidence)
    result = refresh.generate_review_assets(manifest, tmp_path / "generated",
        generate_fn=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("renderer down")))
    assert result[0]["status"] == "failed"
    assert result[0]["reason"] == "RuntimeError"


def test_apply_stale_pending_is_audited_before_any_swap(tmp_path):
    image = tmp_path / "reviewed.png"
    image.write_bytes(b"pixels")
    evidence = {"card.png": {"infographic_copy": {"headline": "Approved", "facts": ["Fact"]}}}
    manifest = refresh.plan([row("a", "2026-09-24")], start="2026-09-24", end="2026-09-24", root=tmp_path,
                            copy_evidence=evidence)
    group = manifest["regeneration_groups"][0]
    review = {"reviewed_groups": [{"id": group["id"], "review_status": "PASS", "path": str(image),
              "image_sha256": __import__("hashlib").sha256(image.read_bytes()).hexdigest()}]}
    class Store:
        def get_row(self, *_):
            return row("a", "2026-09-24", status="approved")
    called = []
    result = refresh.apply_reviewed_swaps(manifest, review, Store(), tmp_path,
        host_fn=lambda *_: called.append(True), review_fn=lambda *_: {"grade_status": "PASS", "image_sha256": review["reviewed_groups"][0]["image_sha256"], "infographic_copy": group["generation"]["approved_copy"]})
    assert result[-1]["status"] == "conflict"
    assert not called
    assert __import__("json").loads((tmp_path / "before-apply.json").read_text())[0]["status"] == "approved"


def test_plan_excludes_lead_owned_11pm_correction(tmp_path):
    old = row("old", "2026-09-24", image="https://cdn/nano_run_ads_at_11pm.png")
    assert refresh.plan([old], start="2026-09-24", end="2026-09-24", root=tmp_path)["regeneration_groups"] == []


def test_apply_stale_image_conflicts_and_keeps_audit(tmp_path):
    image = tmp_path / "reviewed.png"
    image.write_bytes(b"pixels")
    evidence = {"card.png": {"infographic_copy": {"headline": "Approved", "facts": ["Fact"]}}}
    manifest = refresh.plan([row("a", "2026-09-24")], start="2026-09-24", end="2026-09-24", root=tmp_path,
                            copy_evidence=evidence)
    group = manifest["regeneration_groups"][0]
    digest = __import__("hashlib").sha256(image.read_bytes()).hexdigest()
    review = {"reviewed_groups": [{"id": group["id"], "review_status": "PASS", "path": str(image), "image_sha256": digest}]}
    class Store:
        def get_row(self, *_):
            return row("a", "2026-09-24", image="https://cdn/someone-else.png")
    out = refresh.apply_reviewed_swaps(manifest, review, Store(), tmp_path,
        host_fn=lambda *_: (_ for _ in ()).throw(AssertionError("must not host")),
        review_fn=lambda *_: {"grade_status": "PASS", "image_sha256": digest,
                              "infographic_copy": group["generation"]["approved_copy"]})
    assert out[-1]["status"] == "conflict"
    assert __import__("json").loads((tmp_path / "before-apply.json").read_text())[0]["image_url"].endswith("someone-else.png")


def test_host_failure_keeps_before_and_failure_receipt(tmp_path):
    image = tmp_path / "reviewed.png"
    image.write_bytes(b"pixels")
    evidence = {"card.png": {"infographic_copy": {"headline": "Approved", "facts": ["Fact"]}}}
    manifest = refresh.plan([row("a", "2026-09-24")], start="2026-09-24", end="2026-09-24", root=tmp_path,
                            copy_evidence=evidence)
    group = manifest["regeneration_groups"][0]
    digest = __import__("hashlib").sha256(image.read_bytes()).hexdigest()
    review = {"reviewed_groups": [{"id": group["id"], "review_status": "PASS", "path": str(image), "image_sha256": digest}]}
    class Store:
        def get_row(self, *_):
            return row("a", "2026-09-24")
    out = refresh.apply_reviewed_swaps(manifest, review, Store(), tmp_path,
        host_fn=lambda *_: None,
        review_fn=lambda *_: {"grade_status": "PASS", "image_sha256": digest,
                              "infographic_copy": group["generation"]["approved_copy"]})
    assert out[-1]["status"] == "artifact_or_host_failed"
    assert __import__("json").loads((tmp_path / "before-apply.json").read_text())[0]["id"] == "a"
    assert __import__("json").loads((tmp_path / "apply-receipts.json").read_text())[-1]["status"] == "artifact_or_host_failed"
