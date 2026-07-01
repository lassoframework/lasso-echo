"""
Daily Studio tests. The infographic path is dormant unless content brain + creative
studio + hosting are ALL armed. Armed, it produces a hosted PENDING draft whose
source_fragments are all approved source-doc lines (no fabrication). A missing doc
blocks. No network, no real SDKs — fake nano + S3 clients only.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, content_planner, daily_studio  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402


FIXTURE = """# LASSO Now (TEST FIXTURE)

## Story
We help gym owners grow without burning out.

## Pillars
- Speed To Lead
- Retention

## Pillar copy bank

### Pillar: Speed To Lead
Hook: Leads go cold in minutes.
Body: Answer inside five minutes and you book three times more.
Body: Every hour you wait cuts the odds.

### Pillar: Retention
Hook: Keeping a member beats chasing a new one.
Body: A simple onboarding call lifts ninety day retention.

## CTAs
- Save this post for later.
- Tag a gym owner who needs this.

## Hashtags
#LASSOFramework #GymMarketingMadeSimple
"""


class FakeNano:
    def generate_image(self, prompt, model):
        return b"\x89PNG\r\n\x1a\nFAKE"


class FakeS3:
    def __init__(self):
        self.puts = []

    def exists(self, key):
        return False

    def put(self, key, local_path):
        self.puts.append(key)


def _acct():
    return Account(key="lasso_ig", display_name="LASSO IG", platform=Platform.INSTAGRAM,
                   token_env="X", target_id_env="Y")


def _doc(tmp_path):
    p = tmp_path / "lasso_now.md"
    p.write_text(FIXTURE, encoding="utf-8")
    return str(p)


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTENT_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))       # nano writes the image here
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")


def _build(tmp_path, source_path=None):
    return daily_studio.build_daily_infographic_draft(
        _acct(), "2026-07-01", nano_client=FakeNano(), s3_client=FakeS3(),
        source_path=source_path if source_path is not None else _doc(tmp_path))


# ---- 1. dormant when flags off ----------------------------------------------
def test_dormant_when_all_flags_off(monkeypatch, tmp_path):
    for f in ("AGENT_CONTENT_BRAIN_ENABLED", "AGENT_NANO_ENABLED", "AGENT_HOSTING_ENABLED"):
        monkeypatch.delenv(f, raising=False)
    assert _build(tmp_path) is None


def test_dormant_when_one_flag_off(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTENT_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)  # hosting OFF
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    assert _build(tmp_path) is None


# ---- 2. armed produces a hosted PENDING draft -------------------------------
def test_armed_produces_hosted_pending_draft(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    d = _build(tmp_path)
    assert d is not None
    assert d.status == DraftStatus.PENDING
    assert d.creative_public_url.startswith("https://cdn.echo.test/echo/lasso_ig/")
    assert d.caption.strip() != ""
    assert d.hashtags                       # carried from the plan
    assert os.path.isfile(d.creative_path)  # nano wrote the local image


# ---- 3. source_fragments are all approved (no fabrication) ------------------
def test_source_fragments_all_approved(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    path = _doc(tmp_path)
    d = daily_studio.build_daily_infographic_draft(
        _acct(), "2026-07-01", nano_client=FakeNano(), s3_client=FakeS3(), source_path=path)
    approved = content_planner.load_source_doc(path).approved_lines()
    assert d.source_fragments
    for frag in d.source_fragments:
        assert frag in approved, f"fabricated fragment: {frag!r}"


# ---- 4. missing doc blocks --------------------------------------------------
def test_missing_doc_blocks(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    d = _build(tmp_path, source_path=str(tmp_path / "nope.md"))
    assert d.status == DraftStatus.BLOCKED
    assert "missing" in d.blocked_reason.lower()


# ---- 5. the REAL brand_voice/lasso_now.md produces a hosted PENDING draft ----
# (Regression guard: content_planner writes copy_bank as {"hooks","bodies"} and
# daily_studio reads those same keys, so a real pillar's hook + body reach the draft.)
_REAL_DOC = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "brand_voice", "lasso_now.md")


def test_real_source_doc_produces_pending_draft_with_hook_and_body(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    day = "2026-07-01"
    d = daily_studio.build_daily_infographic_draft(
        _acct(), day, nano_client=FakeNano(), s3_client=FakeS3(), source_path=_REAL_DOC)

    assert d is not None
    assert d.status == DraftStatus.PENDING
    assert d.creative_public_url                       # non-empty hosted URL

    # source_fragments must carry the chosen pillar's hook + every body line.
    doc = content_planner.load_source_doc(_REAL_DOC)
    pillar = content_planner.plan_for(day, path=_REAL_DOC)["pillar"]
    block = doc.copy_bank[pillar]
    assert block["hooks"][0] in d.source_fragments     # the hook (headline)
    for body in block["bodies"]:
        assert body in d.source_fragments              # all body lines (facts)


# ---- 6. a doc/parse gap surfaces as BLOCKED, not a silent None ---------------
_HOOK_ONLY_DOC = """# LASSO Now
## Pillars
- Only Hook
## Pillar copy bank
### Pillar: Only Hook
Hook: A hook with no body line.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework
"""


def test_pillar_with_no_body_blocks_not_silent_none(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    p = tmp_path / "hookonly.md"
    p.write_text(_HOOK_ONLY_DOC, encoding="utf-8")
    d = daily_studio.build_daily_infographic_draft(
        _acct(), "2026-07-01", nano_client=FakeNano(), s3_client=FakeS3(), source_path=str(p))
    # Before the fix this returned a silent None (masked as a library fallback).
    assert d is not None
    assert d.status == DraftStatus.BLOCKED
    assert "no approved body lines" in d.blocked_reason.lower()
    assert "only hook" in d.blocked_reason.lower()
