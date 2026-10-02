import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import lasso_daily_summit as summit
from agent.drafter import DraftStatus


DAY = "2026-11-07"


class Artifacts:
    def __init__(self, cached_url=None, claim=True):
        self.cached_url = cached_url
        self.claim_result = claim
        self.saved = []
        self.claimed = []
        self.released = []

    def cached(self, tenant, cache_key):
        self.cached_args = (tenant, cache_key)
        return self.cached_url

    def save(self, tenant, url, path, source, cache_key=None):
        self.saved.append((tenant, url, path, source, cache_key))
        return {"reviewed": True}

    def claim(self, tenant, cache_key, owner):
        self.claimed.append((tenant, cache_key, owner))
        return self.claim_result

    def release(self, tenant, cache_key, owner):
        self.released.append((tenant, cache_key, owner))


def account(key="lasso_ig"):
    return SimpleNamespace(key=key, platform="instagram")


def enabled(day):
    return day == DAY


def test_daily_summit_uses_exact_catalog_copy_and_third_slot():
    artifacts = Artifacts()
    calls = []

    def generate(headline, facts, **kwargs):
        calls.append((headline, facts, kwargs))
        return {"path": "/tmp/summit.png", "route": "test:renderer"}

    draft = summit.build_daily_summit(
        account(), DAY, enabled_fn=enabled, artifact_store=artifacts,
        creative_generate=generate, host_fn=lambda path, tenant, client=None: "https://cdn/summit.png",
    )

    entry = summit._catalog_entry(DAY)
    assert draft.caption == entry["caption"]
    assert draft.infographic_copy == entry["on_image"]
    assert draft.source_fragments == [entry["caption"]]
    assert draft.status is DraftStatus.PENDING
    assert draft.category == "summit"
    assert draft.is_story is False
    assert draft.cadence_slot_index == 2
    assert calls[0][2]["aspect"] == "4:5"
    assert calls[0][2]["cta"] == entry["on_image"]["cta"]
    assert calls[0][2]["art_direction"] == entry["art_direction"]
    assert calls[0][2]["art_direction"] != entry["visual_concept"]
    assert artifacts.saved[0][3]["source_id"] == f"lasso_summit_daily_catalog:{DAY}"
    assert artifacts.saved[0][3]["source_hash"]
    assert len(artifacts.claimed) == len(artifacts.released) == 1
    assert artifacts.claimed[0] == artifacts.released[0]


def test_daily_summit_reuses_only_matching_reviewed_catalog_url(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text('''{"visual_concepts":[{"id":"paper","direction":"Warm paper planning pages"}],"posts":[{"date":"2026-11-07","caption":"Exact caption","visual_concept":"paper","hosted_image_url":"https://cdn/reviewed.png","on_image":{"headline":"Exact headline","facts":["November 7"],"cta":"Claim your seat","footer":"LASSOFRAMEWORK.COM/SUMMIT"}}]}''')
    entry = summit._catalog_entry(DAY, catalog)
    _, cache_key = summit._source_identity(entry)
    artifacts = Artifacts("https://cdn/reviewed.png")

    draft = summit.build_daily_summit(
        account("lasso_fb"), DAY, catalog_path=catalog, enabled_fn=enabled,
        artifact_store=artifacts, creative_generate=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
    )

    assert draft.creative_public_url == "https://cdn/reviewed.png"
    assert draft.creative_path == "https://cdn/reviewed.png"
    assert artifacts.cached_args == ("lasso", cache_key)
    assert artifacts.saved == []


def test_daily_summit_refuses_unreviewed_catalog_url(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text('''{"visual_concepts":[{"id":"paper","direction":"Warm paper planning pages"}],"posts":[{"date":"2026-11-07","caption":"Exact caption","visual_concept":"paper","hosted_image_url":"https://cdn/unreviewed.png","on_image":{"headline":"Exact headline","facts":["November 7"],"cta":"Claim your seat","footer":"LASSOFRAMEWORK.COM/SUMMIT"}}]}''')
    artifacts = Artifacts("https://cdn/different-reviewed.png")
    generated = []
    assert summit.build_daily_summit(
        account(), DAY, catalog_path=catalog, enabled_fn=enabled, artifact_store=artifacts,
        creative_generate=lambda *a, **k: generated.append(True),
    ) is None
    assert generated == []
    assert artifacts.claimed == []


def test_daily_summit_forwards_explicit_art_direction_and_hashes_it(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text('''{"visual_concepts":[{"id":"paper","direction":"Warm paper planning pages"}],"posts":[{"date":"2026-11-07","caption":"Exact caption","visual_concept":"paper","art_direction":"Candid operator hands planning beside a red rule","on_image":{"headline":"Exact headline","facts":["November 7"],"cta":"Claim your seat","footer":"LASSOFRAMEWORK.COM/SUMMIT"}}]}''')
    artifacts, seen = Artifacts(), []

    def generate(*args, **kwargs):
        seen.append(kwargs["art_direction"])
        return {"path": "/tmp/art-direction.png"}

    draft = summit.build_daily_summit(
        account(), DAY, catalog_path=catalog, enabled_fn=enabled, artifact_store=artifacts,
        creative_generate=generate, host_fn=lambda *args, **kwargs: "https://cdn/new.png",
    )

    entry = summit._catalog_entry(DAY, catalog)
    source, _ = summit._source_identity(entry)
    assert draft is not None
    assert seen == ["Candid operator hands planning beside a red rule"]
    assert source["source_hash"] == artifacts.saved[0][3]["source_hash"]


def test_daily_summit_fails_closed_for_demo_flag_off_and_outside_date():
    artifacts = Artifacts()
    for acct, fn, day in ((account("lasso_demo"), enabled, DAY), (account(), lambda _: False, DAY),
                          (account(), enabled, "2026-11-09")):
        assert summit.build_daily_summit(acct, day, enabled_fn=fn, artifact_store=artifacts) is None


def test_daily_summit_lost_claim_rechecks_cache_without_generating():
    class LosingArtifacts(Artifacts):
        def __init__(self):
            super().__init__(claim=False)
            self.reads = 0
        def cached(self, tenant, cache_key):
            self.reads += 1
            return None if self.reads == 1 else "https://cdn/winner.png"

    artifacts = LosingArtifacts()
    draft = summit.build_daily_summit(
        account("lasso_fb"), DAY, enabled_fn=enabled, artifact_store=artifacts,
        creative_generate=lambda *a, **k: (_ for _ in ()).throw(AssertionError("loser generated")),
    )
    assert draft.creative_public_url == "https://cdn/winner.png"
    assert artifacts.reads == 2
    assert len(artifacts.claimed) == 1
    assert artifacts.released == []


def test_daily_summit_lost_claim_holds_when_winner_not_ready():
    artifacts = Artifacts(None, claim=False)
    assert summit.build_daily_summit(
        account(), DAY, enabled_fn=enabled, artifact_store=artifacts,
        creative_generate=lambda *a, **k: (_ for _ in ()).throw(AssertionError("loser generated")),
    ) is None
    assert artifacts.saved == []
    assert artifacts.released == []


def test_daily_summit_rechecks_after_claim_and_releases_without_generation():
    class WonAfterRead(Artifacts):
        def __init__(self):
            super().__init__(); self.reads = 0
        def cached(self, tenant, cache_key):
            self.reads += 1
            return None if self.reads == 1 else "https://cdn/prior-owner.png"

    artifacts = WonAfterRead()
    draft = summit.build_daily_summit(
        account(), DAY, enabled_fn=enabled, artifact_store=artifacts,
        creative_generate=lambda *a, **k: (_ for _ in ()).throw(AssertionError("claim winner regenerated")),
    )
    assert draft.creative_public_url == "https://cdn/prior-owner.png"
    assert artifacts.claimed[0] == artifacts.released[0]


def test_daily_summit_generation_failure_releases_claim():
    artifacts = Artifacts()
    assert summit.build_daily_summit(
        account(), DAY, enabled_fn=enabled, artifact_store=artifacts,
        creative_generate=lambda *a, **k: None,
    ) is None
    assert artifacts.claimed[0] == artifacts.released[0]


def test_daily_summit_ig_fb_share_one_generated_source_artifact():
    class SharedArtifacts(Artifacts):
        def save(self, tenant, url, path, source, cache_key=None):
            result = super().save(tenant, url, path, source, cache_key)
            self.cached_url = url
            return result

    artifacts = SharedArtifacts()
    generated = []
    def generate(*_args, **_kwargs):
        generated.append(True)
        return {"path": "/tmp/one-shared.png", "route": "test"}

    first = summit.build_daily_summit(
        account("lasso_ig"), DAY, enabled_fn=enabled, artifact_store=artifacts,
        creative_generate=generate, host_fn=lambda *_args, **_kwargs: "https://cdn/one-shared.png",
    )
    second = summit.build_daily_summit(
        account("lasso_fb"), DAY, enabled_fn=enabled, artifact_store=artifacts,
        creative_generate=generate, host_fn=lambda *_args, **_kwargs: "https://cdn/should-not-host.png",
    )
    assert generated == [True]
    assert first.creative_public_url == second.creative_public_url == "https://cdn/one-shared.png"
    assert len(artifacts.saved) == 1
    assert len(artifacts.claimed) == len(artifacts.released) == 1
