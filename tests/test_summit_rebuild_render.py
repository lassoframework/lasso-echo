"""
SUMMIT SPRINT render + host loop tests. Offline: no live Gemini, no live R2.

Covers summit_rebuild.render_and_host_all with injected fakes:
  - renders every non-deferred concept FEED (1080x1080) to the EXACT sprint_assets
    filenames, plus the three agenda/panel feed cards, and hosts them;
  - renders the paired 9:16 STORY (1080x1920) for concept cards; agenda/panel skip
    their story honestly (never a cropped feed);
  - writes filename -> URL into the manifest so sprint_assets()/sprint_builders serve;
  - the deferred scarcity concepts (08/09/10 half full / moving fast / last seats)
    are NEVER rendered;
  - re-run is idempotent: already-hosted filenames are skipped (no re-render);
  - an empty-facts concept renders NOTHING (studio None), never faked;
  - the loop is a no-op when the flag is OFF or hosting is OFF.
"""

import os
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import summit_rebuild as srb  # noqa: E402
from agent import summit_queue as sq  # noqa: E402
from agent.summit_rebuild import (  # noqa: E402
    SUMMIT_CONCEPTS, DEFERRED_SCARCITY, FEED_SIZE, STORY_SIZE, AGENDA_PANEL_FILES,
)


# ---- fakes -----------------------------------------------------------------
class FakeStudio:
    """Stand-in for creative_studio. Writes a correctly-sized square PNG and returns
    the generate() result shape. Records every (headline, facts) it saw. Honors the
    no-fabrication contract: empty facts -> None, no file written."""

    def __init__(self, size=FEED_SIZE):
        self.size = size
        self.calls = []

    def generate(self, headline, facts, out_path=None, **kw):
        self.calls.append({"headline": headline, "facts": list(facts or []),
                           "out_path": out_path, "kw": kw})
        if not facts:
            return None
        Image.new("RGB", self.size, (18, 30, 60)).save(out_path)
        return {"path": out_path, "prompt": "fake", "model": "fake"}


def _fake_story(concept, treatment, out_path, **kw):
    Image.new("RGB", STORY_SIZE, (18, 30, 60)).save(out_path)
    return out_path


def _fake_feed_renderer(spec, out_path, **kw):
    Image.new("RGB", FEED_SIZE, (18, 30, 60)).save(out_path)
    return out_path


class FakeHost:
    """Stand-in for media_host.host_media: records (path, bucket) and returns a URL."""

    def __init__(self):
        self.hosted = []

    def __call__(self, path, bucket):
        self.hosted.append((os.path.basename(path), bucket))
        return f"https://r2.example/{bucket}/{os.path.basename(path)}"


class FakeManifest:
    """In-memory manifest store: load/save hooks over a dict, no disk, no summit_queue
    file touched."""

    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.saves = 0

    def load(self):
        return dict(self.data)

    def save(self, data):
        self.data = dict(data)
        self.saves += 1


def _run(images_dir, *, studio=None, host=None, man=None,
         story=_fake_story, agenda=_fake_feed_renderer, panel=_fake_feed_renderer):
    studio = studio or FakeStudio()
    host = host or FakeHost()
    man = man or FakeManifest()
    summary = srb.render_and_host_all(
        str(images_dir), studio=studio, story_renderer=story,
        agenda_renderer=agenda, panel_renderer=panel, host=host,
        load_manifest=man.load, save_manifest=man.save)
    return summary, studio, host, man


@pytest.fixture(autouse=True)
def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_SUMMIT_CAMPAIGN_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")


# ---- filenames match sprint_assets exactly ---------------------------------
def test_hosts_the_18_concept_feed_filenames_matching_sprint_assets(tmp_path):
    _summary, _studio, _host, man = _run(tmp_path)
    concept_feeds = [f"{c['id']}_{t}.png"
                     for c in SUMMIT_CONCEPTS for t in ("a", "b")]
    # every concept feed filename sprint_assets expects is hosted, byte-exact name
    sprint_names = {f for f, _ in sq.sprint_assets()}
    for fname in concept_feeds:
        assert fname in sprint_names, f"{fname} not in sprint_assets"
        assert fname in man.data, f"{fname} missing from manifest"
    # 10 concepts x 2 treatments = 20 concept feed cards
    assert len(concept_feeds) == 20
    assert all(f in man.data for f in concept_feeds)


def test_hosts_agenda_and_panel_feed_and_every_sprint_asset_present(tmp_path):
    _summary, _studio, _host, man = _run(tmp_path)
    for fname in AGENDA_PANEL_FILES:
        assert fname in man.data, f"{fname} missing from manifest"
    # the whole sprint can now serve: every sprint_assets filename has a URL
    for fname, _cap in sq.sprint_assets():
        assert fname in man.data, f"sprint asset unhosted: {fname}"


def test_concept_stories_hosted_paired_1to1(tmp_path):
    _summary, _studio, _host, man = _run(tmp_path)
    for c in SUMMIT_CONCEPTS:
        for t in ("a", "b"):
            story = f"{c['id']}_{t}_story.png"
            assert story in man.data, f"paired story missing: {story}"


# ---- deferred scarcity never rendered --------------------------------------
def test_deferred_scarcity_concepts_never_rendered(tmp_path):
    summary, _studio, host, man = _run(tmp_path)
    assert summary["deferred"] == list(DEFERRED_SCARCITY)
    for cid in DEFERRED_SCARCITY:
        for key in man.data:
            assert not key.startswith(cid), f"deferred concept leaked: {key}"
        for fname, _bucket in host.hosted:
            assert not fname.startswith(cid), f"deferred concept hosted: {fname}"
    # nothing rendered to disk for a deferred id either
    for cid in DEFERRED_SCARCITY:
        assert not os.path.exists(tmp_path / f"{cid}_a.png")


# ---- agenda/panel skip their story honestly --------------------------------
def test_agenda_panel_have_no_story(tmp_path):
    summary, _studio, _host, man = _run(tmp_path)
    for fname in AGENDA_PANEL_FILES:
        stem = os.path.splitext(fname)[0]
        story = f"{stem}_story.png"
        assert story not in man.data, f"agenda/panel story fabricated: {story}"
        assert not os.path.exists(tmp_path / story)
        assert story in summary["skipped_story"]


# ---- feed / story dimensions asserted --------------------------------------
def test_feed_is_1080_square_and_story_is_1080x1920(tmp_path):
    _run(tmp_path)
    feed = Image.open(tmp_path / "01_invitation_a.png")
    assert feed.size == (1080, 1080)
    story = Image.open(tmp_path / "01_invitation_a_story.png")
    assert story.size == (1080, 1920)


def test_native_gemini_size_is_normalized_to_1080_square_before_host(tmp_path):
    """THE LIVE BUG: the Gemini Pro model returns its NATIVE size (e.g. 928x1152),
    NOT the requested 1080x1080. The old fake returned a correct 1080 square and hid
    this; render_and_host_all must NORMALIZE the studio output to the exact canvas
    (house cover-crop, never a squish) BEFORE verify_size, so all assets host."""
    native_studio = FakeStudio(size=(928, 1152))  # the real Gemini Pro native size
    summary, _studio, _host, man = _run(tmp_path, studio=native_studio)
    # a concept feed was hosted (not refused), and it is EXACTLY 1080x1080 on disk
    feed = Image.open(tmp_path / "01_invitation_a.png")
    assert feed.size == (1080, 1080), "studio native size was not normalized to canvas"
    assert "01_invitation_a.png" in man.data, "normalized feed was not hosted"
    assert summary["hosted"], "0 assets hosted (the live bug)"


def test_native_portrait_is_cover_cropped_not_squished(tmp_path):
    """Normalization must COVER (scale by the larger ratio) and center-crop, never
    distort a portrait into a square by a naive resize. A 928x1152 portrait cover-fit
    to a 1080 square scales x1.164 (1080/928, the WIDTH drives it), landing at
    1080x1341, then center-crops to 1080x1080 -- so nothing is squished."""
    native_studio = FakeStudio(size=(928, 1152))
    _summary, _studio, _host, _man = _run(tmp_path, studio=native_studio)
    feed = Image.open(tmp_path / "01_invitation_a.png")
    assert feed.size == (1080, 1080)


def test_native_story_size_is_normalized_to_1080x1920(tmp_path):
    """The story path normalizes too: a story renderer that emits a non-1080x1920
    frame is cover-cropped to the exact 1080x1920 canvas before verify_size + host."""
    def _wrong_story(concept, treatment, out_path, **kw):
        Image.new("RGB", (1000, 1800), (18, 30, 60)).save(out_path)  # not 1080x1920
        return out_path
    _summary, _studio, _host, man = _run(tmp_path, story=_wrong_story)
    story = Image.open(tmp_path / "01_invitation_a_story.png")
    assert story.size == (1080, 1920), "story was not normalized to the canvas"
    assert "01_invitation_a_story.png" in man.data


def test_verify_size_still_refuses_a_wrong_canvas_never_weakened(tmp_path):
    """verify_size is NOT weakened by the normalization: _assert_size still raises on
    any file that is not exactly the expected size. Proven directly so a future change
    that guts the guard is caught."""
    p = tmp_path / "x.png"
    Image.new("RGB", (500, 500), (0, 0, 0)).save(p)
    with pytest.raises(ValueError):
        srb._assert_size(str(p), (1080, 1080), "FEED")


# ---- idempotent re-run -----------------------------------------------------
def test_rerun_is_idempotent_skips_already_hosted(tmp_path):
    _summary, _studio, _host, man = _run(tmp_path)
    first_saves = man.saves
    # second pass over the SAME manifest: nothing new rendered or hosted
    studio2 = FakeStudio()
    host2 = FakeHost()
    summary2 = srb.render_and_host_all(
        str(tmp_path), studio=studio2, story_renderer=_fake_story,
        agenda_renderer=_fake_feed_renderer, panel_renderer=_fake_feed_renderer,
        host=host2, load_manifest=man.load, save_manifest=man.save)
    assert studio2.calls == [], "re-run re-rendered an already-hosted feed"
    assert host2.hosted == [], "re-run re-hosted an already-hosted file"
    assert summary2["hosted"] == []
    assert man.saves == first_saves, "clean re-run must not rewrite the manifest"


def test_rerun_fills_only_the_gaps(tmp_path):
    # seed the manifest with just the first concept feed already hosted
    seeded = {"01_invitation_a.png": "https://r2.example/lasso_summit/01_invitation_a.png"}
    man = FakeManifest(seeded)
    _summary, studio, _host, _man = _run(tmp_path, man=man)
    # the seeded file was not re-rendered (no studio call wrote to that out_path)
    seeded_out_paths = [c for c in studio.calls
                        if "01_invitation_a.png" in (c["out_path"] or "")]
    assert seeded_out_paths == [], "seeded feed was re-rendered on a gap-fill run"
    # but every other concept feed still got hosted
    assert "07_numbers_a.png" in man.data
    assert "01_invitation_a.png" in man.data  # the seeded URL is preserved


# ---- no fabrication: empty facts -> None, never faked ----------------------
def test_empty_facts_concept_renders_nothing(tmp_path, monkeypatch):
    # a concept with no deck / no support and no default facts -> studio gets [] -> None
    empty = {"id": "99_empty", "eyebrow": "X", "headline": "EMPTY", "red_word": "EMPTY",
             "deck": "", "caption": "lassoframework.com/summit"}
    monkeypatch.setattr(srb, "SUMMIT_CONCEPTS", [empty])
    monkeypatch.setattr(srb, "ARC_ORDER", ["99_empty"])
    # neutralize the default facts so the fact list is genuinely empty
    monkeypatch.setattr("agent.summit_render.DEFAULT_FACTS", [])
    studio = FakeStudio()
    host = FakeHost()
    man = FakeManifest()
    summary = srb.render_and_host_all(
        str(tmp_path), studio=studio, story_renderer=_fake_story,
        agenda_renderer=_fake_feed_renderer, panel_renderer=_fake_feed_renderer,
        host=host, load_manifest=man.load, save_manifest=man.save)
    # studio was called (and returned None); nothing hosted for that concept; no story
    assert "99_empty_a.png" in summary["none_facts"]
    assert "99_empty_a.png" not in man.data
    assert "99_empty_a_story.png" not in man.data
    assert not os.path.exists(tmp_path / "99_empty_a.png")


# ---- flag / hosting gates --------------------------------------------------
def test_noop_when_summit_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_SUMMIT_CAMPAIGN_ENABLED", raising=False)
    summary, studio, host, man = _run(tmp_path)
    assert summary["hosted"] == []
    assert studio.calls == []
    assert host.hosted == []
    assert man.data == {}


def test_noop_when_hosting_off(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    summary, studio, host, man = _run(tmp_path)
    assert summary["hosted"] == []
    assert studio.calls == []
    assert man.data == {}


# ---- studio call carries approved input + square target --------------------
def test_studio_called_with_square_target_and_concept_headline(tmp_path):
    _summary, studio, _host, _man = _run(tmp_path)
    first = next(c for c in studio.calls)
    assert first["headline"]  # a real concept headline, not empty
    assert first["kw"].get("pixels") == "1080x1080"
    assert first["kw"].get("aspect") == "1:1"
    assert first["facts"], "facts must be the concept's approved lines, never empty"
