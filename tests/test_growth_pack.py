"""
Phase 1 growth-pack tests: CTA rotation, hashtag cap at 5, carousel support.
Same contracts as the gate tests — no fabrication, no network in draft-only.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import meta_publisher, slack_surface  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import (  # noqa: E402
    Draft, DraftStatus, TemplateGenerator, draft_post, _pick_cta,
)
from agent.library import Creative, list_creatives, _load_carousel  # noqa: E402
from agent.store import PendingStore  # noqa: E402
from agent.voice import VoiceDoc, _extract_ctas  # noqa: E402


# ---- helpers ----------------------------------------------------------------
def _acct(platform=Platform.INSTAGRAM, key="t_ig"):
    return Account(key=key, display_name="T", platform=platform,
                   token_env="T_TOKEN", target_id_env="T_ID")


CTA_DOC_NUMBERED = """
### CTA rotation (cycle in order, one per post)
Some preamble line.

1. Book a free call and we will look at your numbers. Link in bio.
2. Save this post. Come back when your close rate stalls.
3. Send this to a gym owner who needs it.
4. Tag a gym owner who is grinding.

### Hashtag strategy
- Brand: #LASSOFramework
"""

CTA_DOC_QUOTED = '''
### CTA rotation
Rotate through these:
- "Save this post for later."
- "Tag a gym owner who needs this."
- "Save this post for later."
- "[link in bio placeholder]"

## Next section
'''


def _voice_with_ctas():
    return VoiceDoc(
        raw=CTA_DOC_NUMBERED + "\n#LASSOFramework #GymMarketing #GymGrowth "
            "#FitnessBusiness #GymOwnerTips #GymSales #LeadGeneration",
        hashtags=["#LASSOFramework", "#GymMarketing", "#GymGrowth",
                  "#FitnessBusiness", "#GymOwnerTips", "#GymSales",
                  "#LeadGeneration"],
        ctas=_extract_ctas(CTA_DOC_NUMBERED),
    )


# ---- CTA extraction ---------------------------------------------------------
def test_extract_ctas_numbered_list():
    ctas = _extract_ctas(CTA_DOC_NUMBERED)
    assert ctas == [
        "Book a free call and we will look at your numbers. Link in bio.",
        "Save this post. Come back when your close rate stalls.",
        "Send this to a gym owner who needs it.",
        "Tag a gym owner who is grinding.",
    ]


def test_extract_ctas_quoted_skips_brackets_and_dedupes():
    ctas = _extract_ctas(CTA_DOC_QUOTED)
    # quoted strings win; the [bracketed] placeholder is skipped; dupe collapsed
    assert ctas == ["Save this post for later.", "Tag a gym owner who needs this."]


def test_extract_ctas_no_section():
    assert _extract_ctas("no cta section here") == []


# ---- CTA selection: growth-hint preference + determinism --------------------
def test_pick_cta_prefers_growth_hints_and_is_deterministic():
    voice = _voice_with_ctas()
    c = Creative(path="/lib/foo.jpg", media_type="image")
    picked = _pick_cta(voice, c)
    hints = TemplateGenerator.GROWTH_CTA_HINTS
    assert any(h in picked.lower() for h in hints)
    # deterministic across calls for the same stem
    assert _pick_cta(voice, Creative(path="/other/foo.jpg", media_type="image")) == picked


def test_build_appends_cta_only_when_missing():
    voice = _voice_with_ctas()
    # note that ALREADY contains an approved CTA verbatim -> no second CTA appended
    dupe_cta = "Save this post. Come back when your close rate stalls."
    c = Creative(path="/lib/x.jpg", media_type="image",
                 client_note="Great news. " + dupe_cta)
    caption, _, fragments = TemplateGenerator().build(voice, c)
    assert caption.lower().count("save this post") == 1
    assert fragments == [c.client_note]  # nothing appended


# ---- Hashtag cap ------------------------------------------------------------
def test_hashtags_capped_at_five():
    voice = _voice_with_ctas()
    c = Creative(path="/lib/y.jpg", media_type="image", client_note="Hi.")
    _, hashtags, _ = TemplateGenerator().build(voice, c)
    assert len(hashtags) <= TemplateGenerator.HASHTAG_LIMIT == 5
    assert set(hashtags).issubset(set(voice.hashtags))  # nothing invented


# ---- Carousel library loading ----------------------------------------------
def _make_carousel(tmp_path, n=3, note='{"note": "Three slides.", "slide_urls": []}'):
    folder = tmp_path / "my_carousel"
    folder.mkdir()
    for i in range(1, n + 1):
        (folder / f"slide_{i}.png").write_bytes(b"img")
    if note is not None:
        (folder / "note.json").write_text(note, encoding="utf-8")
    return folder


def test_carousel_folder_becomes_one_creative(tmp_path):
    _make_carousel(tmp_path, n=3)
    creatives = list_creatives(str(tmp_path))
    assert len(creatives) == 1
    car = creatives[0]
    assert car.media_type == "carousel"
    assert len(car.slides) == 3
    assert car.client_note == "Three slides."


def test_single_image_folder_is_not_a_carousel(tmp_path):
    folder = tmp_path / "lonely"
    folder.mkdir()
    (folder / "slide_1.png").write_bytes(b"img")
    assert _load_carousel(str(folder)) is None
    assert list_creatives(str(tmp_path)) == []


def test_carousel_folder_note_txt_fallback(tmp_path):
    folder = tmp_path / "c2"
    folder.mkdir()
    (folder / "a.png").write_bytes(b"i")
    (folder / "b.png").write_bytes(b"i")
    (folder / "note.txt").write_text("From a txt note.", encoding="utf-8")
    car = _load_carousel(str(folder))
    assert car.client_note == "From a txt note."


# ---- Draft carries carousel data; store round-trips it ----------------------
def test_draft_carries_slides(tmp_path):
    _make_carousel(tmp_path, n=2,
                   note='{"note": "Two.", "slide_urls": ["https://x/1.png", "https://x/2.png"]}')
    car = list_creatives(str(tmp_path))[0]
    voice = _voice_with_ctas()
    d = draft_post(_acct(), car, "2026-07-01T10:00:00Z", voice=voice)
    assert len(d.slides) == 2
    assert d.slide_urls == ["https://x/1.png", "https://x/2.png"]


def test_store_roundtrips_slides(tmp_path):
    s = PendingStore(path=str(tmp_path / "p.json"))
    d = Draft(draft_id="d1", account_key="k", platform="instagram", caption="c",
              hashtags=[], creative_path="/f", creative_public_url="",
              scheduled_for="t", slides=["/f/a.png", "/f/b.png"],
              slide_urls=["https://x/a", "https://x/b"])
    s.put(d)
    got = s.get("d1")
    assert got.slides == ["/f/a.png", "/f/b.png"]
    assert got.slide_urls == ["https://x/a", "https://x/b"]


# ---- Carousel publish: dormant in draft-only, correct flow when armed -------
def test_carousel_no_network_in_draft_only(monkeypatch):
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)  # OFF

    class ExplodingHTTP:
        def post(self, *a, **k):
            raise AssertionError("Network call in draft-only mode!")

    d = Draft(draft_id="d", account_key="k", platform="instagram", caption="c",
              hashtags=[], creative_path="/f", creative_public_url="",
              scheduled_for="t", slide_urls=["https://x/1", "https://x/2"])
    res = meta_publisher.publish(d, _acct(), http=ExplodingHTTP())
    assert res.mode == "would_publish" and res.ok is True


def test_carousel_publish_flow_when_armed(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("T_TOKEN", "secret")
    monkeypatch.setenv("T_ID", "IG123")

    class CaptureHTTP:
        def __init__(self):
            self.posted = []

        def post(self, url, data=None, timeout=None, **k):
            self.posted.append((url, data))

            class R:
                status_code = 200
                def json(self_inner):
                    return {"id": "CID"}
            return R()

    http = CaptureHTTP()
    d = Draft(draft_id="d", account_key="k", platform="instagram", caption="cap",
              hashtags=[], creative_path="/f", creative_public_url="",
              scheduled_for="t", slide_urls=["https://x/1", "https://x/2"])
    res = meta_publisher.publish(d, _acct(), http=http)
    assert res.mode == "published"
    # 2 child containers + 1 parent + 1 publish = 4 calls
    assert len(http.posted) == 4
    assert any(data.get("is_carousel_item") == "true" for _, data in http.posted)
    assert any(data.get("media_type") == "CAROUSEL" for _, data in http.posted)
    assert http.posted[-1][0].endswith("/media_publish")


# ---- Slack card labels a carousel -------------------------------------------
def test_slack_card_labels_carousel():
    d = Draft(draft_id="d", account_key="lasso_ig", platform="instagram",
              caption="cap", hashtags=["#LASSOFramework"], creative_path="/f",
              creative_public_url="", scheduled_for="t",
              slides=["/f/slide_1.png", "/f/slide_2.png", "/f/slide_3.png"])
    blocks = slack_surface.build_card_blocks(d)
    blob = str(blocks)
    assert "Carousel — 3 slides" in blob
    assert "slide_1.png" in blob and "slide_3.png" in blob
