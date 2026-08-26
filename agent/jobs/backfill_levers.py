"""backfill_levers.py — Wave 7.2 historical lever backfill (best-effort).

Stamps the Wave 7 lever columns (hook_family, ask_type, caption_len_band,
time_slot) onto HISTORICAL content_calendar rows that predate the drafter-time
stamping, using the SAME heuristics (agent/lever_stamp.py) so old and new rows
are classified identically. has_member_face is never backfilled here — it only
ever comes from the vision sidecar, and guessing it would poison the learner.

Behind AGENT_LEARNING_LOOP (default OFF -> no-op). Best-effort: a row that
cannot be classified or patched is skipped and counted, never fatal. READ +
PATCH of lever columns only; captions, statuses, and every other column are
untouched, and nothing here publishes.

Usage:
  python3 -m agent.jobs.backfill_levers            # all gyms
  python3 -m agent.jobs.backfill_levers --gym lasso
"""
from __future__ import annotations

import json

from agent import config
from agent import lever_stamp

LEVER_COLUMNS = ("hook_family", "ask_type", "caption_len_band", "time_slot")


class _RestStore:
    """Minimal PostgREST reader/patcher for the backfill. `http` injectable."""

    def __init__(self, url=None, key=None, http=None):
        self._url = url if url is not None else config.supabase_url()
        self._key = key if key is not None else config.supabase_service_key()
        self._http = http

    def _client(self):
        if self._http is not None:
            return self._http
        import requests
        return requests

    def _headers(self, extra=None):
        h = {"apikey": self._key, "Authorization": f"Bearer {self._key}",
             "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def rows_missing_levers(self, gym_id, limit=1000):
        r = self._client().get(
            f"{self._url}/rest/v1/content_calendar",
            params={"gym_id": f"eq.{gym_id}", "hook_family": "is.null",
                    "select": "id,gym_id,caption,format",
                    "order": "post_date", "limit": str(int(limit))},
            headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            return []
        return r.json() or []

    def patch_levers(self, gym_id, row_id, levers):
        r = self._client().patch(
            f"{self._url}/rest/v1/content_calendar",
            params={"id": f"eq.{row_id}", "gym_id": f"eq.{gym_id}"},
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=minimal"}),
            json=levers, timeout=30)
        return r.status_code < 400


def _default_gyms():
    try:
        from agent.calendar_autopublish import client_gym_bases
        gyms = list(client_gym_bases() or [])
        if "lasso" not in gyms:
            gyms = ["lasso"] + gyms
        return gyms
    except Exception:
        return ["lasso"]


def run(gyms=None, store=None):
    """Backfill lever columns for every row missing them. Behind
    AGENT_LEARNING_LOOP; flag OFF -> no-op with an explanation."""
    if not config.learning_loop_enabled():
        return {"ok": False,
                "reason": "AGENT_LEARNING_LOOP is OFF (default). No backfill."}
    store = store or _RestStore()
    gyms = list(gyms) if gyms else _default_gyms()
    out = []
    for gym_id in gyms:
        stamped = 0
        skipped = 0
        try:
            rows = store.rows_missing_levers(gym_id)
        except Exception:
            out.append({"gym_id": gym_id, "ok": False, "reason": "read failed"})
            continue
        for row in rows:
            try:
                caption = row.get("caption") or ""
                levers = {
                    "hook_family": lever_stamp.hook_family(caption),
                    "ask_type": lever_stamp.ask_type(caption),
                    "caption_len_band": lever_stamp.caption_len_band(caption),
                    "time_slot": lever_stamp.time_slot_band(
                        lever_stamp._default_time_for(row.get("format") or "")),
                }
                if store.patch_levers(gym_id, row.get("id"), levers):
                    stamped += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
        out.append({"gym_id": gym_id, "ok": True,
                    "stamped": stamped, "skipped": skipped})
    return {"ok": True, "gyms": out}


if __name__ == "__main__":
    import sys
    gyms_arg = []
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--gym" and i + 1 < len(args):
            gyms_arg.append(args[i + 1])
            i += 2
        else:
            i += 1
    print(json.dumps(run(gyms=gyms_arg or None), indent=2, default=str))
