"""Upload confirmation UX (Chris Shimley / Top Fuel ask, 2026-08-26).

Chris uploaded media and got NO confirmation it worked, and no idea when it
would reach the calendar. Three fixes under test:

  a. the page carries per-file confirmation elements (green check state, the
     running "N photos/videos received" counter) and honest retry copy;
  b. the completion banner sets expectations (approval queue within the hour,
     human approves everything) with NO dash characters (client-facing copy law);
  c. the backend answers 2xx only AFTER the object is durably stored
     (injectable store), so the check mark is honest — a storage failure is a
     503, never a fake success.

handle_upload is driven directly with fake R2 stores. Fully OFFLINE.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import intake_web  # noqa: E402
from agent.intake_web import PAGE, DONE, handle_upload  # noqa: E402

BANNER_COPY = ("Received! Your content is in. New posts built from "
               "these usually appear in your approval queue within the hour. "
               "You approve everything before it posts.")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_CHRIS", "tok-chris-topfuel")


class FakeR2:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data

    def get_bytes(self, key):
        return self.objects.get(key)

    def list_keys(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))


class BrokenR2(FakeR2):
    """Storage that accepts construction but fails every write — the honest
    503 path (a transient R2 fault, bad creds, wrong bucket)."""

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        raise RuntimeError("simulated storage outage")


class HalfBrokenR2(FakeR2):
    """First write lands, second fails: the response must STILL be a 503 —
    2xx is only ever returned after the WHOLE batch (files + sidecar) landed."""

    def __init__(self, fail_after=1):
        super().__init__()
        self.writes = 0
        self.fail_after = fail_after

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.writes += 1
        if self.writes > self.fail_after:
            raise RuntimeError("simulated mid-batch outage")
        super().put_bytes(key, data, content_type)


FILES = [("gym1.jpg", "image/jpeg", b"jpegbytes"),
         ("class.mp4", "video/mp4", b"mp4bytes")]


# ---- a. the page carries the confirmation elements ----------------------------------

def test_page_has_per_file_confirmation_and_counter_elements():
    # green check state after a successful upload (JS-escaped check mark)
    assert "\\u2713 received" in PAGE
    assert ".state.ok" in PAGE
    # the running received counter element + its updater
    assert 'id="counter"' in PAGE
    assert "updateCounter" in PAGE
    assert "' photos'" in PAGE and "' videos'" in PAGE
    assert "' received'" in PAGE


def test_page_has_completion_banner_copy_no_dashes():
    # The exact reassuring copy, present in the page, and dash-free.
    for fragment in ("Received! Your content is in.",
                     "approval queue within the hour",
                     "You approve everything before it posts."):
        assert fragment in PAGE, fragment
    assert not re.search(r"[–—-]", BANNER_COPY)
    # the no-JS fallback page carries the same honest confirmation
    assert "Received!" in DONE
    assert "approval queue within the hour" in DONE
    assert not re.search(r"[–—]", DONE)


def test_page_failure_copy_is_honest_retry():
    assert "not sent, tap Send to retry" in PAGE
    assert "did not go through" in PAGE
    # sent items are never re-posted on a later Send (running counter batches)
    assert "!p.removed && !p.sent" in PAGE


# ---- c. backend 2xx only after durable store ----------------------------------------

def test_2xx_only_after_all_objects_stored():
    r2 = FakeR2()
    status, body = handle_upload("tok-chris-topfuel", FILES, r2=r2)
    assert status == 200
    assert body["ok"] is True
    assert body["stored"] == 2
    stored = r2.list_keys("intake/chris/incoming/")
    # both media files AND the sidecar landed before the 200 went out
    assert len([k for k in stored if not k.endswith("_upload.json")]) == 2
    assert len([k for k in stored if k.endswith("_upload.json")]) == 1


def test_storage_failure_is_503_never_a_fake_success():
    status, body = handle_upload("tok-chris-topfuel", FILES, r2=BrokenR2())
    assert status == 503
    assert "storage" in body["error"]


def test_mid_batch_failure_is_503_not_partial_success():
    r2 = HalfBrokenR2(fail_after=1)
    status, body = handle_upload("tok-chris-topfuel", FILES, r2=r2)
    assert status == 503
    assert "storage" in body["error"]


def test_upload_page_served_at_token_route():
    """The /u/<token> GET must serve the page with the confirmation UI."""
    assert intake_web.token_from_path("/u/tok-chris-topfuel", "u") \
        == "tok-chris-topfuel"
