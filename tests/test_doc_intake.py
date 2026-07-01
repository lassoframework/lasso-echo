"""
Document intake tests. Fully OFFLINE: no pypdf (raw text is passed via `text=`), no
Gemini (creative_studio + media_host are stubbed via fake clients). Asserts: OFF is a
no-op; ON produces PENDING drafts capped at max_posts; source_fragments trace back to
the client text (no fabrication); a thin idea returns BLOCKED with a reason.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, doc_intake  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


# A client PDF (rendered to text): three message threads, one email, one thin scrap.
FIXTURE = """Thread with Sarah
She said the 6am class changed her mornings and she has more energy now.
She is bringing her sister next week.
===
Thread with Mike
Down 18 pounds in the program and his knees stopped bothering him.
He wants to try the nutrition add on.
===
Thread with the front desk
Three new signups came from the Saturday open house.
Two asked about the small group option.
===
Email from a member
Subject: thank you
Your coaches actually check in on me. I never had that at my old gym.
I renewed for another year.
===
ok
"""


class FakeNano:
    def generate_image(self, prompt, model):
        return b"\x89PNG\r\n\x1a\nFAKE"


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


class _Acct:
    key = "client_demo"
    platform = "instagram"


def _voice():
    return VoiceDoc(raw="We help gym owners grow.\n#LASSOFramework",
                    hashtags=["#LASSOFramework"], ctas=["Save this post."])


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DOC_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")       # creative_studio
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")    # media_host
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")


def _run(**kw):
    return doc_intake.process_document(
        text=FIXTURE, account=_Acct(), voice=_voice(),
        nano_client=FakeNano(), s3_client=FakeS3(), **kw)


# ---- 1. flag OFF -> dormant no-op -------------------------------------------
def test_flag_off_is_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_DOC_INTAKE_ENABLED", raising=False)
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    assert _run() is None


# ---- 2. flag ON -> PENDING drafts, thin idea BLOCKED ------------------------
def test_flag_on_produces_pending_drafts_and_blocks_thin(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    drafts = _run()
    assert drafts is not None
    pending = [d for d in drafts if d.status == DraftStatus.PENDING]
    blocked = [d for d in drafts if d.status == DraftStatus.BLOCKED]
    assert len(pending) == 4       # 3 threads + 1 email
    assert len(blocked) == 1       # the "ok" scrap
    assert "thin" in blocked[0].blocked_reason.lower()
    # every PENDING draft got a hosted infographic + a caption
    for d in pending:
        assert d.creative_public_url.startswith("https://cdn.echo.test/echo/client_demo/")
        assert d.caption.strip() != ""


# ---- 3. max_posts caps the number of drafts --------------------------------
def test_max_posts_caps_output(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    drafts = _run(max_posts=2)
    assert len(drafts) == 2
    assert all(d.status == DraftStatus.PENDING for d in drafts)


# ---- 4. no fabrication: source_fragments trace back to the client text ------
def test_source_fragments_trace_to_client_text(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    drafts = _run()
    for d in drafts:
        if d.status != DraftStatus.PENDING:
            continue
        assert d.source_fragments
        for frag in d.source_fragments:
            assert frag in FIXTURE, f"fabricated fragment not in client text: {frag!r}"


# ---- 5. a thin idea alone returns exactly one BLOCKED draft with a reason ----
def test_thin_idea_blocks(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    drafts = doc_intake.process_document(
        text="hi", account=_Acct(), voice=_voice(),
        nano_client=FakeNano(), s3_client=FakeS3())
    assert len(drafts) == 1
    assert drafts[0].status == DraftStatus.BLOCKED
    assert "honest" in drafts[0].blocked_reason.lower()
