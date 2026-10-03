"""Focused caller contract for the opt-in global visual writer ledger."""

from agent import media_swap, portal_social
from agent import config


class SwapStore:
    def __init__(self):
        self.row = {"id": "p1", "gym_id": "gym", "status": "pending",
                    "format": "feed", "image_url": "https://cdn/old.jpg",
                    "post_date": "2026-10-03", "account": "instagram",
                    "caption": "kept caption"}
        self.writes = []

    def get_row(self, account, row_id):
        return dict(self.row) if account == "gym" and row_id == "p1" else None

    def list_month(self, account, month):
        return [dict(self.row)] if account == "gym" else []

    def swap_media(self, account, row_id, image_url, **kwargs):
        self.writes.append((account, row_id, image_url, kwargs))
        self.row["image_url"] = image_url
        return dict(self.row)


def _enable(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    monkeypatch.setattr(portal_social, "_action_gates", lambda *a, **k: None)
    monkeypatch.setattr(portal_social, "_published_is_final", lambda *a: None)
    monkeypatch.setattr(portal_social, "_month_rows_for", lambda *a: [])
    monkeypatch.setattr(media_swap, "enabled", lambda: True)
    monkeypatch.setattr(media_swap, "after_swap", lambda *a, **k: None)
    monkeypatch.setattr(portal_social.config, "portal_calendar_supabase_enabled",
                        lambda: True)


def test_portal_propagates_raw_source_and_render_evidence(monkeypatch):
    _enable(monkeypatch)
    store = SwapStore()
    evidence = {"source_exact_url": "https://cdn/raw.jpg",
                "delivered_exact_url": "https://cdn/crop.jpg",
                "operation": "render"}
    pick = {"ok": True, "image_url": evidence["delivered_exact_url"],
            "source_media_url": evidence["source_exact_url"], "render_evidence": evidence,
            "kind": "photo", "source": "local", "key": "raw.jpg",
            "thumbnail_url": "", "source_media_asset_id": "", "siblings": {}}

    status, _ = portal_social.handle_swap_media("gym", "p1", "actor", sb_store=store,
                                                picker=lambda *a, **k: pick)

    assert status == 200
    kwargs = store.writes[0][3]
    assert kwargs["source_media_url"] == evidence["source_exact_url"]
    assert kwargs["render_evidence"] == evidence


def test_portal_fails_closed_when_derived_variant_lacks_evidence(monkeypatch):
    _enable(monkeypatch)
    store = SwapStore()
    pick = {"ok": True, "image_url": "https://cdn/crop.jpg",
            "source_media_url": "https://cdn/raw.jpg", "kind": "photo",
            "source": "local", "key": "raw.jpg", "thumbnail_url": "",
            "source_media_asset_id": "", "siblings": {}}

    status, body = portal_social.handle_swap_media("gym", "p1", "actor", sb_store=store,
                                                   picker=lambda *a, **k: pick)

    assert status == 409
    assert body["reason"] == "media_evidence_unavailable"
    assert store.writes == []


def test_feed_render_evidence_requires_hosted_bytes_to_match_local_render(monkeypatch, tmp_path):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"source")
    monkeypatch.setattr(media_swap, "_visual_writer_enabled", lambda: True)
    monkeypatch.setattr(config, "feed_autofit_enabled", lambda: False)
    from agent import visual_writer_prepare
    monkeypatch.setattr(visual_writer_prepare, "_bytes_for_url",
                        lambda url: {"https://cdn/raw.jpg": b"source",
                                     "https://cdn/rendered.jpg": b"different-hosted-object"}.get(url))

    evidence = media_swap._feed_render_evidence(
        str(source), "https://cdn/raw.jpg", "https://cdn/rendered.jpg",
        rendered_bytes=b"local-rendered-object")
    assert evidence is None
