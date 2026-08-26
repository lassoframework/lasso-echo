"""demographics_sync.py — per-gym engaged-audience demographics
(flag AGENT_AUDIENCE_DEMOGRAPHICS, default OFF).

Weekly per gym: pull Zernio's Instagram demographics for the gym's connected
IG account — BOTH kinds, follower_demographics and engaged_audience_demographics
(age / city / country / gender breakdowns) — and store each as one
gym_audience_demographics row (gym_id, captured_at, kind, breakdown jsonb).

WEEKLY GATE: the runner calls this daily; a per-gym kv stamp
(`demographics_sync_<gym>`, epoch seconds — the zernio_link_ts pattern) makes
it a no-op until 7 days have passed for that gym. The stamp is written only
after a successful store write, so a failed week retries the next day.

HARD RULES:
- READ ONLY on the social side (one GET per kind; nothing written to Zernio).
- No invented data: a gym with no Zernio profile, no IG account, or a failed
  pull is REPORTED and skipped — never guessed, never zero-filled. The
  breakdown stored is Zernio's response verbatim.
- Storage is upsert-by-PK (gym_id, captured_at, kind): a re-run the same day
  refreshes, never duplicates.

Fully injectable (zernio, store, kv, now); run(gyms=None) callable standalone:
    python3 -m agent.jobs.demographics_sync [--gym topfuel]
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent import config
from agent.zernio import ZernioClient, instagram_account_id

WEEK_SECONDS = 7 * 86400

# (kind column value, Zernio metric name)
KINDS = (("followers", "follower_demographics"),
         ("engaged", "engaged_audience_demographics"))


# ---- default store (PostgREST; injectable) -----------------------------------------


class DemographicsStoreError(Exception):
    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"gym_audience_demographics {status}: {detail}")


class SupabaseDemographicsStore:
    """PostgREST client for gym_audience_demographics writes and reads.
    `http` injectable (the metrics_sync store pattern)."""

    def __init__(self, url=None, service_key=None, http=None):
        self._url = url if url is not None else config.supabase_url()
        self._key = service_key if service_key is not None else config.supabase_service_key()
        self._http = http

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy, repo pattern
        return requests

    def _headers(self, extra=None):
        h = {"apikey": self._key, "Authorization": f"Bearer {self._key}",
             "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def upsert_rows(self, rows):
        """UPSERT on the (gym_id, captured_at, kind) primary key: a same-day
        re-run refreshes the row instead of erroring. Returns rows sent."""
        if not rows:
            return 0
        r = self._client().post(
            f"{self._url}/rest/v1/gym_audience_demographics",
            params={"on_conflict": "gym_id,captured_at,kind"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal"}),
            json=rows, timeout=30)
        if r.status_code >= 400:
            raise DemographicsStoreError(r.status_code, (r.text or "")[:200])
        return len(rows)

    def latest(self, gym_id, kind="engaged"):
        """The newest stored breakdown row for a gym and kind, or None."""
        r = self._client().get(
            f"{self._url}/rest/v1/gym_audience_demographics",
            params={"gym_id": f"eq.{gym_id}", "kind": f"eq.{kind}",
                    "order": "captured_at.desc", "limit": "1"},
            headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            return None
        rows = r.json() or []
        return rows[0] if rows else None


# ---- pure helpers -------------------------------------------------------------------


def build_rows(gym_id, captured_at, pulls):
    """gym_audience_demographics rows from {kind: demographics_response}.
    Only kinds whose response actually carries a demographics object become
    rows — an empty or failed pull stores NOTHING (no invented data)."""
    rows = []
    for kind, resp in sorted((pulls or {}).items()):
        demo = (resp or {}).get("demographics")
        if not isinstance(demo, dict) or not demo:
            continue
        rows.append({"gym_id": gym_id, "captured_at": captured_at,
                     "kind": kind, "breakdown": demo})
    return rows


def top_bucket(breakdown, dimension):
    """(name, pct) of the largest bucket in one dimension of a stored
    breakdown, or None. Tolerates both {dim: {bucket: n}} and
    {dim: [{name/value, count/value}]} response shapes; pct is the bucket's
    share of the dimension total, rounded to a whole percent."""
    dim = (breakdown or {}).get(dimension)
    pairs = []
    if isinstance(dim, dict):
        pairs = [(str(k), v) for k, v in dim.items()
                 if isinstance(v, (int, float)) and not isinstance(v, bool)]
    elif isinstance(dim, list):
        for item in dim:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("value") or item.get("dimension")
            count = item.get("count") if isinstance(item.get("count"), (int, float)) \
                else item.get("total") if isinstance(item.get("total"), (int, float)) \
                else None
            if name is not None and count is not None and not isinstance(count, bool):
                pairs.append((str(name), count))
    total = sum(v for _n, v in pairs)
    if not pairs or total <= 0:
        return None
    name, count = max(pairs, key=lambda kv: kv[1])
    return name, round(100.0 * count / total)


def digest_line(engaged_row):
    """The one retro-digest line from a STORED engaged row, or None when no
    row exists. Only cites the stored breakdown, never a guess. Client-facing
    copy law: no dash characters (age bands render '25 to 34')."""
    if not engaged_row:
        return None
    breakdown = engaged_row.get("breakdown") or {}
    gender = top_bucket(breakdown, "gender")
    age = top_bucket(breakdown, "age")
    parts = []
    if gender:
        name = {"F": "women", "M": "men", "U": "unspecified"}.get(
            str(gender[0]).upper(), str(gender[0]))
        parts.append(f"{gender[1]}% {name}")
    if age:
        band = str(age[0]).replace("-", " to ")
        parts.append(f"peak {band}")
    if not parts:
        return None
    return "Engaged audience: " + ", ".join(parts)


# ---- run ----------------------------------------------------------------------------


def _default_gyms():
    try:
        from agent.calendar_autopublish import client_gym_bases
        gyms = list(client_gym_bases() or [])
        if "lasso" not in gyms:
            gyms = ["lasso"] + gyms
        return gyms
    except Exception:
        return ["lasso"]


def sync_gym(gym_id, zernio, store, now):
    """One gym's pull + store. Reported-not-guessed on every failure leg."""
    profile_id = zernio.find_profile_id(gym_id)
    if not profile_id:
        return {"gym_id": gym_id, "ok": False,
                "reason": "no Zernio profile for gym (reported, not guessed)"}
    try:
        accounts_json = zernio.list_accounts(profile_id)
    except Exception as exc:  # noqa: BLE001
        return {"gym_id": gym_id, "ok": False,
                "reason": f"accounts pull failed: {type(exc).__name__}"}
    ig_account = instagram_account_id(accounts_json)
    if not ig_account:
        return {"gym_id": gym_id, "ok": False,
                "reason": "no connected Instagram account"}
    pulls = {}
    errors = []
    for kind, metric in KINDS:
        try:
            pulls[kind] = zernio.instagram_demographics(ig_account, metric=metric)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{kind}: {type(exc).__name__}")
    rows = build_rows(gym_id, now.date().isoformat(), pulls)
    if not rows:
        return {"gym_id": gym_id, "ok": False,
                "reason": "no demographics returned"
                          + (f" ({'; '.join(errors)})" if errors else ""),
                "errors": errors}
    written = store.upsert_rows(rows)
    return {"gym_id": gym_id, "ok": True, "rows_written": written,
            "kinds": sorted(r["kind"] for r in rows), "errors": errors}


def run(gyms=None, zernio=None, store=None, now=None, kv_get=None, kv_set=None):
    """The weekly sync. Behind AGENT_AUDIENCE_DEMOGRAPHICS (default OFF ->
    no-op, nothing constructed, no network touched). Called daily by the
    runner; the per-gym 7-day kv gate makes it weekly. One gym's failure
    never blocks the rest."""
    if not config.audience_demographics_enabled():
        return {"ok": False, "reason": "AGENT_AUDIENCE_DEMOGRAPHICS is OFF "
                                       "(default). No pull performed.", "gyms": []}
    now = now or datetime.now(timezone.utc)
    zernio = zernio or ZernioClient()
    store = store or SupabaseDemographicsStore()
    if kv_get is None or kv_set is None:
        from agent import db as _db
        kv_get = kv_get or _db.kv_get
        kv_set = kv_set or _db.kv_set
    gyms = list(gyms) if gyms else _default_gyms()

    results = []
    for gym_id in gyms:
        stamp = f"demographics_sync_{gym_id}"
        try:
            last = float(kv_get(stamp) or 0)
        except (TypeError, ValueError):
            last = 0.0
        if now.timestamp() - last < WEEK_SECONDS:
            results.append({"gym_id": gym_id, "ok": True,
                            "skipped": "synced within the last 7 days"})
            continue
        try:
            summary = sync_gym(gym_id, zernio, store, now)
            if summary.get("ok"):
                # stamp only on success so a failed week retries tomorrow
                kv_set(stamp, str(now.timestamp()))
            results.append(summary)
        except Exception as exc:  # noqa: BLE001
            results.append({"gym_id": gym_id, "ok": False,
                            "reason": f"sync failed: {type(exc).__name__}"})
    return {"ok": True, "gyms": results}


if __name__ == "__main__":
    import json
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
