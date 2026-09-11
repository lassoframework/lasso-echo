"""
echo_clients_cleanup.py — undo what the 2026-09-11 fleet sweep created for gyms that
are NOT Echo clients.

    python -m agent echo-clients-cleanup                 # dry run (default): a table
    python -m agent echo-clients-cleanup --apply         # archive to <DATA_DIR>/_trash/<date>/
    python -m agent echo-clients-cleanup --keep a,b      # extra bases to protect, by hand

WHAT IT JUDGES (each its own row in the table)
  registry_row   a gym_accounts.json entry (autoregister put ~110 there)
  brand_voice    <DATA_DIR>/brand_voice/<key>/   (the website-intake sweep wrote bibles)
  content_lib    <LIBRARY_PATH>/<key>/
  echo_gyms      a shared-plane echo_gyms row   (gym-store-sync pushed 140 on 2026-09-10)
  local_gyms     a row in this service's local sqlite `gyms` table (the other half of
                 the echo_gyms mirror; left in place it would be pushed back)

THE DECISION, per item, in this order
  keep     hardcoded accounts.ACCOUNTS base (lasso, district_h, eng, gritx, topfuel,
           blake_personal) -- a human wrote it into the repo
  keep     listed in --keep
  keep     an Echo client: the item's gym_id (registry rows carry one) or its key is
           in the client universe (echo_gym_settings + every alias of those 20 gyms,
           both key derivations included, so toughtemple086f51 AND toughtemple52040e
           both survive)
  remove   a KNOWN non-client: its gym_id or key belongs to a portal gym that has NO
           echo_gym_settings row (the set the incident actually iterated)
  unknown  neither -- e.g. a directory that is not a gym at all (content_library/
           book_campaign), a legacy alias the plane never carried ('districth'), a
           test fixture. LEFT ALONE and listed for a human. This is what keeps the
           command from ever touching LASSO's own assets at the content_library root.

RAILS
  * Dry run is the default and prints the table. Nothing moves without --apply.
  * REFUSES (exit 2, nothing touched) when the client universe cannot be read: a
    failed predicate is not "no clients", and deleting on it would delete everything.
  * Never deletes: --apply MOVES directories and writes removed rows as JSON under
    <DATA_DIR>/_trash/<YYYY-MM-DD>/, copies the registry to a timestamped .bak first,
    and only then rewrites it (accounts.remove_gyms, locked + atomic).
  * echo_gyms rows are archived as JSON before the DELETE; local gyms rows likewise.
"""
import json
import os
import shutil
import sys
from datetime import datetime, timezone

from . import config, echo_clients

KEEP, REMOVE, UNKNOWN = "keep", "remove", "unknown"
KINDS = ("registry_row", "brand_voice", "content_lib", "echo_gyms", "local_gyms")


def _ts(epoch):
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _dir_times(path):
    """(created, modified) for a path, best effort. macOS exposes a birth time; Linux
    (the Railway box) does not, so 'created' falls back to the inode change time."""
    try:
        st = os.stat(path)
    except OSError:
        return "", ""
    born = getattr(st, "st_birthtime", None) or st.st_ctime
    return _ts(born), _ts(st.st_mtime)


def decide(key, gym_id, universe, *, hard, keep):
    """The one decision function. Returns (verdict, reason)."""
    k = echo_clients.normalize_key(key)
    gid = echo_clients.normalize_key(gym_id)
    if k in hard:
        return KEEP, "hardcoded accounts.ACCOUNTS base"
    if k in keep:
        return KEEP, "--keep"
    if gid and universe.is_client(gid):
        return KEEP, f"Echo client (gym_id -> {universe.names.get(gid, gid)})"
    if k and universe.is_client(k):
        owner = universe.key_to_gym.get(k, "")
        return KEEP, f"Echo client key ({universe.names.get(owner, owner) or 'alias'})"
    if gid and gid in universe.other_gym_ids:
        return REMOVE, "portal gym with no echo_gym_settings row (gym_id)"
    if k and k in universe.other_keys:
        return REMOVE, "portal gym with no echo_gym_settings row (key)"
    return UNKNOWN, "not a gym the shared plane knows; left alone, review by hand"


def _registry_items(universe, hard, keep, *, rows=None, path=None):
    from . import accounts
    path = path or config.gym_registry_path()
    rows = accounts._load_registry_rows() if rows is None else rows   # noqa: SLF001
    created, modified = _dir_times(path)
    out = []
    for r in rows:
        base = str(r.get("base") or "").strip()
        if not base:
            continue
        verdict, reason = decide(base, r.get("gym_id"), universe, hard=hard, keep=keep)
        out.append({"kind": "registry_row", "key": base, "name": str(r.get("name") or ""),
                    "gym_id": str(r.get("gym_id") or ""), "created": "(file) " + created,
                    "modified": "(file) " + modified, "verdict": verdict,
                    "reason": reason, "path": path, "row": r})
    return out


def _dir_items(kind, root, universe, hard, keep):
    out = []
    if not root or not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if not os.path.isdir(full) or name.startswith((".", "_")):
            continue
        verdict, reason = decide(name, "", universe, hard=hard, keep=keep)
        created, modified = _dir_times(full)
        out.append({"kind": kind, "key": name, "name": "", "gym_id": "",
                    "created": created, "modified": modified, "verdict": verdict,
                    "reason": reason, "path": full})
    return out


def _echo_gyms_items(universe, hard, keep, *, rows):
    out = []
    for r in rows or []:
        key = str(r.get("account_key") or "").strip()
        if not key:
            continue
        verdict, reason = decide(key, "", universe, hard=hard, keep=keep)
        out.append({"kind": "echo_gyms", "key": key,
                    "name": str(r.get("display_name") or r.get("gym_name") or ""),
                    "gym_id": "", "created": str(r.get("created_at") or "")[:16],
                    "modified": str(r.get("updated_at") or "")[:16], "verdict": verdict,
                    "reason": reason, "path": "supabase:echo_gyms", "row": r})
    return out


def _local_gyms_items(universe, hard, keep, *, rows):
    out = []
    for r in rows or []:
        key = str(r.get("account_key") or "").strip()
        if not key:
            continue
        verdict, reason = decide(key, "", universe, hard=hard, keep=keep)
        out.append({"kind": "local_gyms", "key": key,
                    "name": str(r.get("display_name") or r.get("gym_name") or ""),
                    "gym_id": "", "created": str(r.get("created_at") or "")[:16],
                    "modified": str(r.get("updated_at") or "")[:16], "verdict": verdict,
                    "reason": reason, "path": "sqlite:gyms", "row": r})
    return out


def plan(*, universe=None, keep=(), registry_rows=None, registry_path=None,
         voice_root=None, library_root=None, shared_rows=None, local_rows=None):
    """Build the dry-run plan. Every reader is injectable; the defaults are live.
    Returns {"ok", "error", "items", "universe"}; ok=False means REFUSE."""
    universe = universe if universe is not None else echo_clients.snapshot(fresh=True)
    if not universe.ok:
        return {"ok": False, "error": f"client universe unreadable ({universe.error}); "
                "refusing to judge anything", "items": [], "universe": universe}
    hard = echo_clients.hardcoded_bases()
    keep = {echo_clients.normalize_key(k) for k in (keep or ()) if str(k or "").strip()}
    items = []
    items += _registry_items(universe, hard, keep, rows=registry_rows, path=registry_path)
    items += _dir_items("brand_voice", voice_root if voice_root is not None
                        else config.client_voice_dir(), universe, hard, keep)
    items += _dir_items("content_lib", library_root if library_root is not None
                        else config.LIBRARY_PATH, universe, hard, keep)
    if shared_rows is None:
        shared_rows = _live_shared_rows()
    items += _echo_gyms_items(universe, hard, keep, rows=shared_rows)
    if local_rows is None:
        local_rows = _live_local_rows()
    items += _local_gyms_items(universe, hard, keep, rows=local_rows)
    return {"ok": True, "error": "", "items": items, "universe": universe}


def _live_shared_rows():
    try:
        from .gym_shared_store import SharedGymStore
        store = SharedGymStore()
        return store.list_all() if store.available() else []
    except Exception as e:  # noqa: BLE001 - listed as an error row, never a crash
        print(f"[echo-clients-cleanup] echo_gyms unreadable: {type(e).__name__}: {e}")
        return []


def _live_local_rows():
    try:
        from . import db
        return db.gym_list(_shared_read=False)
    except Exception as e:  # noqa: BLE001
        print(f"[echo-clients-cleanup] local gyms unreadable: {type(e).__name__}: {e}")
        return []


def format_table(result):
    """Plain aligned text; no markdown tables (Blake's rule)."""
    u = result.get("universe")
    lines = []
    if not result.get("ok"):
        lines.append(f"REFUSED: {result.get('error')}")
        return "\n".join(lines)
    lines.append(f"Echo client universe: {len(u.gym_ids)} gyms (echo_gym_settings), "
                 f"{len(u.keys)} client keys/aliases, {len(u.other_keys)} non-client "
                 f"portal keys")
    items = result["items"]
    cols = ("kind", "key", "name", "gym_id", "created", "modified", "verdict", "reason")
    cap = {c: (64 if c == "reason" else 44) for c in cols}
    widths = {c: len(c) for c in cols}
    for it in items:
        for c in cols:
            widths[c] = min(max(widths[c], len(str(it.get(c, "")))), cap[c])
    def _row(vals):
        return "  ".join(str(v)[:cap[c]].ljust(widths[c]) for c, v in zip(cols, vals))
    lines.append("")
    lines.append(_row(cols))
    lines.append(_row(["-" * widths[c] for c in cols]))
    for verdict in (REMOVE, UNKNOWN, KEEP):
        for it in items:
            if it["verdict"] == verdict:
                lines.append(_row([it.get(c, "") for c in cols]))
    counts = {v: sum(1 for it in items if it["verdict"] == v) for v in (KEEP, REMOVE, UNKNOWN)}
    by_kind = {}
    for it in items:
        if it["verdict"] == REMOVE:
            by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
    lines.append("")
    lines.append(f"keep {counts[KEEP]}   remove {counts[REMOVE]} "
                 f"({', '.join(f'{k} {n}' for k, n in sorted(by_kind.items())) or 'none'})"
                 f"   unknown/left alone {counts[UNKNOWN]}")
    return "\n".join(lines)


def _trash_root(date=None):
    day = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(config.data_dir(), "_trash", day)


def apply(result, *, trash_root=None, store=None, delete_local=None, remove_gyms=None):
    """Archive-then-remove every REMOVE item. Refuses on a plan that is not ok. Returns
    {"trash", "moved", "registry_removed", "echo_gyms_deleted", "local_deleted",
    "errors"}. Every step is per item and an error on one never stops the rest."""
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "plan not ok")
    trash = trash_root or _trash_root()
    os.makedirs(trash, exist_ok=True)
    out = {"trash": trash, "moved": [], "registry_removed": [], "echo_gyms_deleted": [],
           "local_deleted": [], "errors": []}
    removes = [it for it in result["items"] if it["verdict"] == REMOVE]

    # 1. directories: MOVE into the trash (never rmtree the source)
    for it in removes:
        if it["kind"] not in ("brand_voice", "content_lib"):
            continue
        dest_dir = os.path.join(trash, it["kind"])
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, it["key"])
        try:
            if os.path.exists(dest):
                dest = f"{dest}.{datetime.now(timezone.utc).strftime('%H%M%S')}"
            shutil.move(it["path"], dest)
            out["moved"].append((it["path"], dest))
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{it['kind']} {it['key']}: {type(e).__name__}: {e}")

    # 2. registry rows: archive the rows, then one locked atomic rewrite
    reg = [it for it in removes if it["kind"] == "registry_row"]
    if reg:
        try:
            with open(os.path.join(trash, "gym_accounts.removed.json"), "w",
                      encoding="utf-8") as fh:
                json.dump([it["row"] for it in reg], fh, indent=2)
            if remove_gyms is None:
                from .accounts import remove_gyms as remove_gyms
            removed = remove_gyms([it["key"] for it in reg], backup_dir=trash)
            out["registry_removed"] = [str(r.get("base") or "") for r in removed]
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"registry: {type(e).__name__}: {e}")

    # 3. shared-plane echo_gyms rows: archive JSON, then DELETE by account_key
    shared = [it for it in removes if it["kind"] == "echo_gyms"]
    if shared:
        if store is None:
            from .gym_shared_store import SharedGymStore
            store = SharedGymStore()
        arc = os.path.join(trash, "echo_gyms")
        os.makedirs(arc, exist_ok=True)
        for it in shared:
            try:
                with open(os.path.join(arc, f"{it['key']}.json"), "w", encoding="utf-8") as fh:
                    json.dump(it["row"], fh, indent=2)
                store.delete(it["key"])
                out["echo_gyms_deleted"].append(it["key"])
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"echo_gyms {it['key']}: {type(e).__name__}: {e}")

    # 4. local sqlite gyms rows: archive JSON, then DELETE by account_key
    local = [it for it in removes if it["kind"] == "local_gyms"]
    if local:
        if delete_local is None:
            delete_local = _delete_local_gym
        arc = os.path.join(trash, "gyms_local")
        os.makedirs(arc, exist_ok=True)
        for it in local:
            try:
                with open(os.path.join(arc, f"{it['key']}.json"), "w", encoding="utf-8") as fh:
                    json.dump(it["row"], fh, indent=2, default=str)
                delete_local(it["key"])
                out["local_deleted"].append(it["key"])
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"local gyms {it['key']}: {type(e).__name__}: {e}")

    try:
        from . import db
        db.audit("echo_clients_cleanup", "apply",
                 f"moved {len(out['moved'])} dirs, removed {len(out['registry_removed'])} "
                 f"registry rows, deleted {len(out['echo_gyms_deleted'])} echo_gyms + "
                 f"{len(out['local_deleted'])} local rows -> {trash}", "lasso", "")
    except Exception:  # noqa: BLE001
        pass
    return out


def _delete_local_gym(account_key):
    from . import db
    with db._lock, db.connect() as conn:   # noqa: SLF001
        conn.execute("DELETE FROM gyms WHERE account_key = ?", (account_key,))
        conn.commit()


def main(argv=None):
    argv = list(argv or [])
    do_apply = "--apply" in argv
    keep = ()
    if "--keep" in argv:
        i = argv.index("--keep")
        if i + 1 < len(argv):
            keep = tuple(k.strip() for k in argv[i + 1].split(",") if k.strip())
    result = plan(keep=keep)
    print(format_table(result))
    if not result["ok"]:
        return 2
    if not do_apply:
        print("\nDRY RUN. Nothing was moved. Re-run with --apply to archive the REMOVE "
              "rows to " + _trash_root() + " (unknown rows are never touched).")
        return 0
    out = apply(result)
    print(f"\nAPPLIED -> {out['trash']}")
    print(f"  moved dirs         : {len(out['moved'])}")
    print(f"  registry rows      : {len(out['registry_removed'])}")
    print(f"  echo_gyms deleted  : {len(out['echo_gyms_deleted'])}")
    print(f"  local gyms deleted : {len(out['local_deleted'])}")
    for e in out["errors"]:
        print(f"  ERROR {e}")
    return 1 if out["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
