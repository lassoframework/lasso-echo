"""Bounded operator for legacy Echo registry rows missing their portal gym UUID.

Run ``python -m agent.registry_identity_backfill`` for a read-only plan. Applying
requires the dry-run's original, new and mapping hashes plus the observed
Railway commit. This edits only missing gym_id fields in the existing JSON,
preserving every other byte and row order. It never publishes or sends.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, echo_clients

_BASE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_LEGACY_BASES = frozenset({
    "piercefitness", "theboltonclub", "crossfitlocal", "hillcountry",
    "crossfitreverb30b5b2", "crossfitnewtown", "toughtemple52040e",
    "swiftrivercrossfite5c9db", "crossfitsunnysidef574c0",
})


class BackfillBlocked(RuntimeError):
    """An incomplete identity or changed file prevents the whole operation."""


@dataclass(frozen=True)
class BackfillPlan:
    original_sha256: str
    new_sha256: str
    mappings_sha256: str
    mappings: tuple
    row_count: int
    new_bytes: bytes


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise BackfillBlocked(f"registry JSON has duplicate key {key!r}")
        out[key] = value
    return out


def _space(text, at):
    while at < len(text) and text[at] in " \t\r\n":
        at += 1
    return at


def _object_fields(text, start, end, decoder):
    """Top-level property value spans inside one already decoded row object."""
    fields = {}
    at = _space(text, start + 1)
    while at < end and text[at] != "}":
        key, key_end = decoder.raw_decode(text, at)
        if not isinstance(key, str):
            raise BackfillBlocked("registry object has a non-string key")
        at = _space(text, key_end)
        if at >= end or text[at] != ":":
            raise BackfillBlocked("registry object field separator is invalid")
        value_start = _space(text, at + 1)
        _, value_end = decoder.raw_decode(text, value_start)
        fields[key] = (value_start, value_end)
        at = _space(text, value_end)
        if at < end and text[at] == ",":
            at = _space(text, at + 1)
        elif at < end and text[at] != "}":
            raise BackfillBlocked("registry object field boundary is invalid")
    if at != end - 1:
        raise BackfillBlocked("registry object boundary is invalid")
    return fields


def _decode_registry(raw):
    """Parse strictly and return rows with source spans for byte-preserving edits."""
    try:
        text = raw.decode("utf-8")
        decoder = json.JSONDecoder(object_pairs_hook=_unique_object)
        start = _space(text, 0)
        rows, end = decoder.raw_decode(text, start)
        if _space(text, end) != len(text) or not isinstance(rows, list):
            raise BackfillBlocked("registry must be exactly one JSON array")
        if text[start] != "[":
            raise BackfillBlocked("registry must be a JSON array")
        spans = []
        at = _space(text, start + 1)
        while at < end and text[at] != "]":
            row_start = at
            row, row_end = decoder.raw_decode(text, at)
            if not isinstance(row, dict) or text[row_start] != "{":
                raise BackfillBlocked("registry row must be a JSON object")
            spans.append((row_start, row_end,
                          _object_fields(text, row_start, row_end, decoder)))
            at = _space(text, row_end)
            if at < end and text[at] == ",":
                at = _space(text, at + 1)
            elif at < end and text[at] != "]":
                raise BackfillBlocked("registry array boundary is invalid")
        if at != end - 1 or len(spans) != len(rows):
            raise BackfillBlocked("registry array spans do not match its rows")
        return text, rows, spans
    except (UnicodeDecodeError, ValueError, IndexError, TypeError) as exc:
        raise BackfillBlocked("registry JSON is unreadable or malformed") from exc


def _registry_identities(rows):
    """Validate the whole file, returning indexes of genuinely missing IDs."""
    bases, ids, missing = set(), set(), []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise BackfillBlocked("registry contains a non-object row")
        base = row.get("base")
        if not isinstance(base, str) or not _BASE.fullmatch(base):
            raise BackfillBlocked("registry contains an invalid base")
        if base in bases:
            raise BackfillBlocked("registry contains duplicate bases")
        bases.add(base)
        raw_gid = row.get("gym_id")
        if raw_gid is None or (isinstance(raw_gid, str) and not raw_gid.strip()):
            missing.append(index)
            continue
        if not isinstance(raw_gid, str):
            raise BackfillBlocked("registry contains an invalid gym_id")
        gid = raw_gid.strip().lower()
        if not echo_clients._UUID_RE.fullmatch(gid):  # noqa: SLF001
            raise BackfillBlocked("registry contains an invalid gym_id")
        if gid in ids:
            raise BackfillBlocked("registry contains duplicate gym IDs")
        ids.add(gid)
    return missing, ids


def _fresh_clients(snapshot_fn, clock):
    try:
        clients = snapshot_fn(fresh=True)
    except Exception as exc:  # noqa: BLE001 - a source outage must never permit an edit
        raise BackfillBlocked("fresh authoritative Echo client snapshot unavailable") from exc
    now = clock()
    if (clients is None or not clients.ok or not clients.at or now - clients.at > 120
            or clients.at - now > 30):
        raise BackfillBlocked("fresh authoritative Echo client snapshot unavailable")
    return clients


def _bound_owner(base, clients):
    """A name/slug alias is not evidence that a registry base belongs to a gym."""
    gid = clients.key_to_gym.get(base)
    if not isinstance(gid, str) or not echo_clients._UUID_RE.fullmatch(gid):  # noqa: SLF001
        return None
    issued = base in clients.valid_token_keys_by_gym.get(gid, ())
    name = clients.names.get(gid, "")
    derived = (gid in clients.valid_gym_row_ids
               and gid not in clients.invalid_gym_row_ids and bool(name)
               and base in (echo_clients._echo_key(gid, name),  # noqa: SLF001
                            echo_clients._portal_key(gid, name)))  # noqa: SLF001
    if (base not in clients.keys or base in clients.ambiguous_keys
            or base in clients.other_keys or base in clients.invalid_owner_keys
            or gid not in clients.gym_ids
            or gid not in clients.valid_marker_ids
            or gid in clients.invalid_marker_ids
            or gid in clients.invalid_gym_row_ids
            or not clients.markers.get(gid) or not clients.is_client(gid)
            or not (issued or derived)):
        return None
    return gid


def build_plan(raw, clients):
    """Pure plan: all rows and mappings validated before constructing any edit."""
    if not clients.ok:
        raise BackfillBlocked("Echo client universe is unreadable")
    text, rows, spans = _decode_registry(raw)
    missing, assigned_ids = _registry_identities(rows)
    mappings = []
    for index in missing:
        base = rows[index]["base"]
        if base not in _LEGACY_BASES:
            raise BackfillBlocked(f"missing-ID base {base!r} is outside legacy scope")
        gid = _bound_owner(base, clients)
        if gid is None:
            raise BackfillBlocked(f"missing-ID base {base!r} has no unique Echo owner")
        if gid in assigned_ids:
            raise BackfillBlocked("two registry rows would own one gym UUID")
        assigned_ids.add(gid)
        mappings.append({"row_index": index, "base": base, "gym_id": gid,
                         "markers": sorted(clients.markers[gid])})

    edits = []
    for mapping in mappings:
        index, gid = mapping["row_index"], mapping["gym_id"]
        start, end, fields = spans[index]
        encoded = json.dumps(gid, separators=(",", ":"))
        if "gym_id" in fields:
            value_start, value_end = fields["gym_id"]
            edits.append((value_start, value_end, encoded))
        else:
            insertion = end - 1
            while insertion > start and text[insertion - 1] in " \t\r\n":
                insertion -= 1
            if text[insertion - 1] == "{":
                raise BackfillBlocked("registry row has no field to append to")
            edits.append((insertion, insertion, ',"gym_id":' + encoded))
    new_text = text
    for start, end, replacement in sorted(edits, reverse=True):
        new_text = new_text[:start] + replacement + new_text[end:]
    new_raw = new_text.encode("utf-8")
    _, new_rows, _ = _decode_registry(new_raw)
    still_missing, _ = _registry_identities(new_rows)
    if still_missing or len(new_rows) != len(rows):
        raise BackfillBlocked("planned registry did not pass strict validation")
    expected = [dict(row) for row in rows]
    for mapping in mappings:
        expected[mapping["row_index"]]["gym_id"] = mapping["gym_id"]
    if new_rows != expected:
        raise BackfillBlocked("planned edit changed a field outside missing gym_id")
    mapping_raw = json.dumps(mappings, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
    return BackfillPlan(_sha(raw), _sha(new_raw), _sha(mapping_raw),
                        tuple(mappings), len(rows), new_raw)


@contextlib.contextmanager
def _registry_lock(path):
    """Require the same sibling advisory lock used by accounts.register_gym."""
    lock_path = str(path) + ".lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise BackfillBlocked("registry advisory lock unavailable") from exc
    lock = os.fdopen(fd, "r+b")
    acquired = False
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            acquired = True
        except OSError as exc:
            raise BackfillBlocked("registry advisory lock unavailable") from exc
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def _require_regular_registry(path):
    # lstat, not stat: a symlink must never redirect a guarded registry write.
    if not stat.S_ISREG(path.lstat().st_mode):
        raise BackfillBlocked("registry target is not a regular file")


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path, raw, mode):
    fd, temp_path = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _write_private(path, raw):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())


def _recover_receipt(path, current_raw, clients, *, original_sha, new_sha,
                     mappings_sha, deployed_commit):
    """Finish an interrupted receipt only after reconstructing its entire plan."""
    pattern = (path.name + ".identity-backfill-*-" + original_sha[:12]
               + ".receipt.json")
    receipts = list(path.parent.glob(pattern))
    if len(receipts) != 1:
        raise BackfillBlocked("applied registry has no unique recovery receipt")
    receipt_path = receipts[0]
    backup = Path(str(receipt_path)[:-len(".receipt.json")] + ".backup")
    _require_regular_registry(receipt_path)
    _require_regular_registry(backup)
    try:
        receipt = json.loads(receipt_path.read_bytes(), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        raise BackfillBlocked("recovery receipt is malformed") from exc
    if not isinstance(receipt, dict):
        raise BackfillBlocked("recovery receipt is malformed")
    backup_raw = backup.read_bytes()
    prior = build_plan(backup_raw, clients)
    if (prior.original_sha256 != original_sha or prior.new_sha256 != new_sha
            or prior.mappings_sha256 != mappings_sha or prior.new_bytes != current_raw
            or not prior.mappings):
        raise BackfillBlocked("recovery backup does not reproduce the applied registry")
    expected_fields = {
        "registry_path": str(path), "backup_path": str(backup),
        "row_count": prior.row_count, "mappings": list(prior.mappings),
        "mappings_sha256": mappings_sha, "original_sha256": original_sha,
        "backup_sha256": original_sha, "new_sha256": new_sha,
        "expected_deployed_commit": deployed_commit,
        "observed_deployed_commit": deployed_commit,
    }
    if any(receipt.get(key) != value for key, value in expected_fields.items()):
        raise BackfillBlocked("recovery receipt disagrees with its backup or plan")
    if receipt.get("status") == "prepared":
        receipt["status"] = "applied"
        receipt["readback_sha256"] = new_sha
        _write_new(receipt_path,
                   (json.dumps(receipt, indent=2) + "\n").encode("utf-8"), 0o600)
        status = "recovered"
    elif receipt.get("status") == "applied" and receipt.get("readback_sha256") == new_sha:
        _fsync_dir(path.parent)  # also settles a prior receipt-replace fsync failure
        status = "already_applied"
    else:
        raise BackfillBlocked("recovery receipt has an invalid status or readback")
    return {"status": status, "backup": str(backup), "receipt": str(receipt_path),
            "receipt_sha256": _sha(receipt_path.read_bytes())}


def execute(*, apply=False, expected_original_sha256=None, expected_new_sha256=None,
            expected_mappings_sha256=None, expected_deployed_commit=None,
            path=None, snapshot_fn=None, clock=None, environ=None):
    """Dry-run or guarded apply. Returns a plan and optional backup/receipt paths."""
    path = Path(path or config.gym_registry_path())
    snapshot_fn = snapshot_fn or echo_clients.snapshot
    clock = clock or time.time
    environ = environ if environ is not None else os.environ
    if apply:
        if not all(isinstance(value, str) and _SHA256.fullmatch(value)
                   for value in (expected_original_sha256, expected_new_sha256,
                                 expected_mappings_sha256)):
            raise BackfillBlocked("apply requires the three dry-run SHA-256 values")
        if (not isinstance(expected_deployed_commit, str)
                or not _COMMIT.fullmatch(expected_deployed_commit.lower())):
            raise BackfillBlocked("apply requires the exact deployed commit SHA")
        observed = str(environ.get("RAILWAY_GIT_COMMIT_SHA") or "").lower()
        if observed != expected_deployed_commit.lower():
            raise BackfillBlocked("deployed commit differs from apply expectation")
    try:
        if not apply:
            clients = _fresh_clients(snapshot_fn, clock)
            _require_regular_registry(path)
            plan = build_plan(path.read_bytes(), clients)
            return {"status": "dry_run", "plan": plan}
        _require_regular_registry(path)
        with _registry_lock(path):
            # A contended lock may have delayed apply beyond the snapshot TTL.
            # Resolve owners only after acquiring the registry writer's lock.
            clients = _fresh_clients(snapshot_fn, clock)
            _require_regular_registry(path)
            raw = path.read_bytes()
            plan = build_plan(raw, clients)
            if (plan.original_sha256 == expected_new_sha256
                    and not plan.mappings):
                result = _recover_receipt(
                    path, raw, clients, original_sha=expected_original_sha256,
                    new_sha=expected_new_sha256,
                    mappings_sha=expected_mappings_sha256,
                    deployed_commit=expected_deployed_commit.lower())
                return {"plan": plan, **result}
            if (plan.original_sha256 != expected_original_sha256
                    or plan.new_sha256 != expected_new_sha256
                    or plan.mappings_sha256 != expected_mappings_sha256):
                raise BackfillBlocked("registry or ownership plan changed since dry-run")
            if not plan.mappings:
                return {"status": "already_applied", "plan": plan}

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            prefix = path.with_name(path.name + ".identity-backfill-" + stamp
                                    + "-" + plan.original_sha256[:12])
            backup = Path(str(prefix) + ".backup")
            receipt_path = Path(str(prefix) + ".receipt.json")
            _write_private(backup, raw)
            _fsync_dir(path.parent)
            if _sha(backup.read_bytes()) != plan.original_sha256:
                raise BackfillBlocked("before-state backup checksum failed")
            receipt = {
                "status": "prepared", "timestamp_utc": stamp,
                "registry_path": str(path), "backup_path": str(backup),
                "row_count": plan.row_count, "mappings": list(plan.mappings),
                "mappings_sha256": plan.mappings_sha256,
                "original_sha256": plan.original_sha256,
                "backup_sha256": _sha(backup.read_bytes()),
                "new_sha256": plan.new_sha256,
                "expected_deployed_commit": expected_deployed_commit.lower(),
                "observed_deployed_commit": observed,
                "rollback": ("Stop Echo publisher; verify current registry SHA-256 equals "
                             + plan.new_sha256 + "; acquire " + str(path) + ".lock; "
                             "copy the exact backup bytes to a sibling temp file, fsync, "
                             "atomically replace the registry, and verify SHA-256 equals "
                             + plan.original_sha256 + ". Resume only after an independent "
                             "readback and owner decision."),
            }
            _write_private(receipt_path,
                           (json.dumps(receipt, indent=2) + "\n").encode("utf-8"))
            _fsync_dir(path.parent)
            _require_regular_registry(path)
            mode = stat.S_IMODE(path.stat().st_mode)
            _write_new(path, plan.new_bytes, mode)
            _require_regular_registry(path)
            readback = path.read_bytes()
            if _sha(readback) != plan.new_sha256:
                raise BackfillBlocked("post-write registry checksum failed; see backup and receipt")
            _, rows, _ = _decode_registry(readback)
            missing, _ = _registry_identities(rows)
            if missing or len(rows) != plan.row_count:
                raise BackfillBlocked("post-write registry validation failed; see backup and receipt")
            for mapping in plan.mappings:
                if rows[mapping["row_index"]].get("gym_id") != mapping["gym_id"]:
                    raise BackfillBlocked("post-write identity readback failed; see backup and receipt")
            receipt["status"] = "applied"
            receipt["readback_sha256"] = _sha(readback)
            # Preserve the prepared receipt until the applied version is fsynced.
            _write_new(receipt_path,
                       (json.dumps(receipt, indent=2) + "\n").encode("utf-8"),
                       0o600)
            return {"status": "applied", "plan": plan, "backup": str(backup),
                    "receipt": str(receipt_path),
                    "receipt_sha256": _sha(receipt_path.read_bytes())}
    except OSError as exc:
        raise BackfillBlocked(f"registry I/O failed: {type(exc).__name__}") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-original-sha256")
    parser.add_argument("--expect-new-sha256")
    parser.add_argument("--expect-mappings-sha256")
    parser.add_argument("--expect-deployed-commit")
    args = parser.parse_args(argv)
    try:
        result = execute(
            apply=args.apply, expected_original_sha256=args.expect_original_sha256,
            expected_new_sha256=args.expect_new_sha256,
            expected_mappings_sha256=args.expect_mappings_sha256,
            expected_deployed_commit=args.expect_deployed_commit)
        plan = result["plan"]
        print(json.dumps({"status": result["status"], "row_count": plan.row_count,
                          "mappings": plan.mappings,
                          "original_sha256": plan.original_sha256,
                          "new_sha256": plan.new_sha256,
                          "mappings_sha256": plan.mappings_sha256,
                          "backup": result.get("backup"),
                          "receipt": result.get("receipt"),
                          "receipt_sha256": result.get("receipt_sha256")}, indent=2))
        return 0
    except BackfillBlocked as exc:
        print(f"BLOCKED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
