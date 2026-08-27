"""caption_trace.py — where did the caption go? Pure logging, zero behavior.

trace_publish(row_id, platform) is a context manager yielding a tracer whose
t(stage, value) logs one line per publish stage with the value's VISIBLE
character count (publish_guard.visible_len). Stages, in publish order:

    row_loaded -> caption_resolved -> platform_payload_built -> api_request

The FIRST time a non-empty caption becomes empty between stages it logs
    CAPTION LOST <stage> row=<id> platform=<platform>
so a caption dropped by payload assembly (the empty-IG-caption class) is
grep-able to the exact stage. Nothing here blocks, retries, or mutates —
tracing a publish can never change it, and a tracer error never raises into
the publish lane.
"""
from __future__ import annotations

from contextlib import contextmanager

STAGES = ("row_loaded", "caption_resolved", "platform_payload_built", "api_request")


class _Tracer:
    def __init__(self, row_id, platform):
        self.row_id = row_id
        self.platform = platform
        self._seen_nonempty = False
        self._lost_logged = False

    def t(self, stage, value):
        """Log one stage. `value` is the caption/body as of this stage."""
        try:
            from .publish_guard import visible_len
            n = visible_len(value)
            print(f"[caption-trace] row={self.row_id} platform={self.platform} "
                  f"stage={stage} visible={n}")
            if n == 0 and self._seen_nonempty and not self._lost_logged:
                self._lost_logged = True
                print(f"CAPTION LOST {stage} row={self.row_id} "
                      f"platform={self.platform}")
            if n > 0:
                self._seen_nonempty = True
        except Exception:
            pass  # pure logging: a tracer error never touches the publish lane


@contextmanager
def trace_publish(row_id, platform):
    """Context manager for one row's publish attempt. Yields the tracer; never
    swallows the publish path's own exceptions and never raises its own."""
    yield _Tracer(row_id, platform)
