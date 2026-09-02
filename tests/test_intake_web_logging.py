"""
The intake web service's logs must actually REACH Railway.

2026-09-02: chasing a live client bug (The Bolton Club could not connect Google Business),
`railway logs --service echo-intake-web` returned nothing but container-start lines. Cause:
Python block-buffers stdout when it is not a TTY, this service prints rarely and never with
flush=True, and a low-traffic HTTP server never fills an 8KB buffer -- so a real client's
failed attempt was written to a buffer that in practice never flushed. A client-serving
service whose logs are unreadable is worse than one that is merely noisy.
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import intake_web as iw  # noqa: E402


class _Reconfigurable(io.StringIO):
    def __init__(self):
        super().__init__()
        self.line_buffering_set = None

    def reconfigure(self, **kw):
        self.line_buffering_set = kw.get("line_buffering")


def test_line_buffering_is_actually_requested_on_both_streams(monkeypatch):
    out, err = _Reconfigurable(), _Reconfigurable()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    iw.line_buffer_stdio()
    assert out.line_buffering_set is True
    assert err.line_buffering_set is True, "stderr matters too: tracebacks are the logs"


def test_a_stream_without_reconfigure_never_crashes_the_service(monkeypatch):
    """An older Python, or a wrapped stream, must degrade to today's behavior rather than
    take the whole web service down on boot over a logging nicety."""
    class _NoReconfigure(io.StringIO):
        pass

    monkeypatch.setattr(sys, "stdout", _NoReconfigure())
    monkeypatch.setattr(sys, "stderr", _NoReconfigure())
    iw.line_buffer_stdio()   # must not raise


def test_it_is_idempotent(monkeypatch):
    out = _Reconfigurable()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _Reconfigurable())
    iw.line_buffer_stdio()
    iw.line_buffer_stdio()
    assert out.line_buffering_set is True
