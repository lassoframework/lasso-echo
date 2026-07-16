"""
Tests for voice_template (Stage 2 T3) and the tenant brain wiring in approvals.
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_draft(account_key="gym_test", caption="Original caption",
                creative_path="library/some_card.jpg"):
    """Return a minimal mock Draft."""
    from agent.drafter import Draft, DraftStatus
    d = MagicMock(spec=Draft)
    d.account_key = account_key
    d.caption = caption
    d.creative_path = creative_path
    d.draft_id = "draft_abc123"
    d.status = DraftStatus.PENDING
    d.platform = "instagram"
    d.hashtags = []
    d.scheduled_for = "2026-07-05"
    return d


# ---------------------------------------------------------------------------
# voice_template tests
# ---------------------------------------------------------------------------

class TestVoiceTemplate(unittest.TestCase):

    def _render_to_tmp(self):
        from agent.voice_template import render_template
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tf:
            tmp = tf.name
        try:
            render_template(out_path=tmp)
            with open(tmp, encoding="utf-8") as fh:
                return fh.read()
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_template_is_dash_free(self):
        """Rendered output must contain no em dash (U+2014) or en dash (U+2013)."""
        content = self._render_to_tmp()
        self.assertNotIn("—", content, "em dash found in rendered template")
        self.assertNotIn("–", content, "en dash found in rendered template")

    def test_template_has_required_sections(self):
        """All 8 section names must appear in the rendered output."""
        from agent.voice_template import TEMPLATE_SECTIONS
        content = self._render_to_tmp()
        for section in TEMPLATE_SECTIONS:
            self.assertIn(section["name"], content,
                          f"section '{section['name']}' missing from template")

    def test_template_no_vendor(self):
        """The word 'vendor' must not appear anywhere in the rendered template."""
        content = self._render_to_tmp()
        self.assertNotIn("vendor", content.lower(),
                         "word 'vendor' found in rendered template")

    def test_template_writes_to_path(self):
        """render_template(out_path=...) must create a non-empty file at that path."""
        from agent.voice_template import render_template
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tf:
            tmp = tf.name
        try:
            result = render_template(out_path=tmp)
            self.assertEqual(result, tmp)
            self.assertTrue(os.path.exists(tmp))
            self.assertGreater(os.path.getsize(tmp), 100)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


# ---------------------------------------------------------------------------
# Brain wiring in approvals tests
# ---------------------------------------------------------------------------

class TestApproveBrainWiring(unittest.TestCase):
    """Verify that handle_action records brain events when the flag is ON."""

    def _handle(self, action, draft, note="", redraft_fn=None,
                publisher=None, extra_env=None):
        """Call handle_action with AGENT_TENANT_BRAIN_ENABLED=true."""
        env = {"AGENT_TENANT_BRAIN_ENABLED": "true"}
        if extra_env:
            env.update(extra_env)
        with patch.dict(os.environ, env):
            from agent import approvals
            # reset module-level streak state between calls
            approvals._streak_counters.clear()
            from agent.approvals import handle_action
            return handle_action(
                action, draft,
                actor_slack_id="U06EPUUCL13",
                note=note,
                redraft_fn=redraft_fn,
                publisher=publisher,
                account=None,
            )

    def test_approve_streak_records_brain(self):
        """Three consecutive approves must produce approve_streak brain entries."""
        import importlib
        # Set up a fake publisher that always succeeds
        fake_pub = MagicMock()
        fake_pub.publish.return_value = MagicMock(mode="would_publish", media_id="m1", post_id="")

        # Set up a fake account
        fake_acct = MagicMock()
        fake_acct.key = "gym_test"
        fake_acct.platform = "instagram"
        fake_acct.approver_ids.return_value = []

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "AGENT_TENANT_BRAIN_ENABLED": "true",
                "AGENT_ENABLED": "true",
            }
            with patch.dict(os.environ, env):
                with patch("agent.approvals.get_account", return_value=fake_acct):
                    with patch("agent.postlog.log_post"):
                        with patch("agent.publish_confirm.confirm_publish"):
                            with patch("agent.tenant_brain.brains_dir", return_value=tmpdir):
                                from agent import approvals
                                approvals._streak_counters.clear()

                                from agent.approvals import handle_action
                                for _ in range(3):
                                    d = _make_draft()
                                    handle_action(
                                        "approve", d,
                                        actor_slack_id="U06EPUUCL13",
                                        publisher=fake_pub,
                                        account=fake_acct,
                                    )

                                from agent.tenant_brain import read_events
                                with patch("agent.tenant_brain.brains_dir", return_value=tmpdir):
                                    events = read_events("gym_test", base_dir=tmpdir)

            streak_events = [e for e in events if e["kind"] == "approve_streak"]
            self.assertGreaterEqual(len(streak_events), 1,
                                    "expected at least one approve_streak brain entry")

    def test_edit_records_style_rule(self):
        """An edit action must produce an edit_diff brain entry with before, after, rule keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"AGENT_TENANT_BRAIN_ENABLED": "true"}
            with patch.dict(os.environ, env):
                with patch("agent.tenant_brain.brains_dir", return_value=tmpdir):
                    def fake_redraft(draft, note):
                        new = _make_draft(caption="Edited caption")
                        return new

                    from agent.approvals import handle_action
                    from agent import approvals
                    approvals._streak_counters.clear()

                    d = _make_draft(caption="Before caption")
                    with patch("agent.approvals.get_account", return_value=MagicMock(
                        key="gym_test", approver_ids=lambda: []
                    )):
                        handle_action(
                            "edit", d,
                            actor_slack_id="U06EPUUCL13",
                            note="Make it shorter",
                            redraft_fn=fake_redraft,
                        )

                    from agent.tenant_brain import read_events
                    with patch("agent.tenant_brain.brains_dir", return_value=tmpdir):
                        events = read_events("gym_test", base_dir=tmpdir)

            edit_events = [e for e in events if e["kind"] == "edit_diff"]
            self.assertEqual(len(edit_events), 1, "expected one edit_diff brain entry")
            ev = edit_events[0]
            self.assertIn("before", ev)
            self.assertIn("after", ev)
            self.assertIn("rule", ev)

    def test_brain_records_only_style_not_claims(self):
        """If a deny note contains a stat claim, prompt_notes must not emit it as a fact."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"AGENT_TENANT_BRAIN_ENABLED": "true"}
            with patch.dict(os.environ, env):
                with patch("agent.tenant_brain.brains_dir", return_value=tmpdir):
                    from agent.approvals import handle_action
                    from agent import approvals
                    approvals._streak_counters.clear()

                    d = _make_draft()
                    with patch("agent.approvals.get_account", return_value=MagicMock(
                        key="gym_test", approver_ids=lambda: []
                    )):
                        handle_action(
                            "deny", d,
                            actor_slack_id="U06EPUUCL13",
                            note="Do not mention 50% off promo",
                        )

                    from agent.tenant_brain import read_events
                    with patch("agent.tenant_brain.brains_dir", return_value=tmpdir):
                        events = read_events("gym_test", base_dir=tmpdir)

            deny_events = [e for e in events if e["kind"] == "deny_reason"]
            self.assertEqual(len(deny_events), 1, "expected one deny_reason brain entry")

            # The deny reason IS recorded (that is correct), but prompt_notes must
            # gate it through the fabrication filter. The % claim makes it a
            # candidate for gating: we verify the brain entries do NOT bypass the
            # gate by checking the deny reason is recorded but the prompt_notes
            # path (which calls rotation.is_gate_clean) filters it before prompts.
            # Here we confirm the deny note was stored correctly.
            self.assertIn("50%", deny_events[0].get("reason", ""),
                          "deny reason with stat claim should be stored (gate filters at prompt time)")

            # And that prompt_notes WOULD filter it: mock is_gate_clean to reject the line.
            with patch("agent.tenant_brain.brains_dir", return_value=tmpdir):
                with patch("agent.rotation.is_gate_clean", return_value=False):
                    from agent.tenant_brain import prompt_notes
                    with patch.dict(os.environ, env):
                        notes = prompt_notes("gym_test", base_dir=tmpdir)
            self.assertEqual(notes, [],
                             "prompt_notes must return empty when gate rejects a stat claim")


if __name__ == "__main__":
    unittest.main()
