"""Exact historical row classification and opt-in bounded Zernio readback.

Offline mode verifies the page hashes in a saved MANIFEST_INDEX and joins only
by each calendar row's own late_post_id. Capture mode performs sequential GETs
to /v1/posts/{id}; it never downloads media or writes to Zernio.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import multiprocessing
import tempfile
import time

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_SAFE_POST_ID = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_page(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"page path escapes evidence root: {rel}")
    return path


def classify_index(index_path: Path, evidence_root: Path):
    """Verify indexed published pages and produce exact, deterministic rows."""
    index = _load_json(index_path)
    group = (index.get("groups") or {}).get("published") or {}
    files = group.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("index has no published page list")
    rows = []
    pages = []
    seen_ids = Counter()
    for item in files:
        rel = item.get("path")
        if not isinstance(rel, str):
            raise ValueError("published page entry has no path")
        path = _safe_page(evidence_root, rel)
        raw = path.read_bytes()
        actual = _sha256(raw)
        if actual != item.get("sha256"):
            raise ValueError(f"sha256 mismatch for {rel}")
        envelope = json.loads(raw)
        page_rows = envelope.get("rows") if isinstance(envelope, dict) else None
        if not isinstance(page_rows, list):
            raise ValueError(f"page has no rows array: {rel}")
        if len(page_rows) != item.get("rows"):
            raise ValueError(f"row count mismatch for {rel}")
        pages.append({"path": rel, "sha256": actual, "rows": len(page_rows)})
        for offset, row in enumerate(page_rows):
            if not isinstance(row, dict):
                rows.append({"record_type": "row", "classification": "malformed_row",
                             "row_id": None, "late_post_id": None,
                             "source_page": rel, "page_offset": offset,
                             "source_media_url": None, "source_media_asset_id": None})
                continue
            row_id = row.get("id")
            post_id = row.get("late_post_id")
            valid_id = isinstance(post_id, str) and bool(_SAFE_POST_ID.fullmatch(post_id.strip()))
            status = row.get("status")
            classification = ("exact_post_lookup_candidate" if valid_id else
                              "missing_exact_post_id")
            if isinstance(post_id, str) and post_id.strip() and not valid_id:
                classification = "unsafe_exact_post_id"
            seen_ids[str(row_id)] += 1 if row_id is not None else 0
            rows.append({
                "record_type": "row", "classification": classification,
                "row_id": row_id, "gym_id": row.get("gym_id"),
                "account": row.get("account"), "post_date": row.get("post_date"),
                "status": status, "late_post_id": post_id.strip() if valid_id else None,
                "source_page": rel, "page_offset": offset,
                "source_media_url": row.get("source_media_url"),
                "source_media_asset_id": row.get("source_media_asset_id"),
                "source_lineage": "explicit_calendar_fields_only",
            })
    duplicates = sorted(k for k, count in seen_ids.items() if count > 1)
    if duplicates:
        duplicate_set = set(duplicates)
        for row in rows:
            if str(row.get("row_id")) in duplicate_set:
                row["classification"] = "duplicate_calendar_row_id"
    summary = {
        "record_type": "summary", "index_sha256": _sha256(index_path.read_bytes()),
        "project_id": index.get("project_id"),
        "snapshot_scope": index.get("snapshot_scope"),
        "pages": pages, "row_count": len(rows),
        "unique_row_ids": len(seen_ids), "duplicate_row_ids": duplicates,
        "classification_counts": dict(sorted(Counter(r["classification"] for r in rows).items())),
        "lineage_policy": "No inference from dates, captions, URLs, account, or ordering.",
    }
    return [summary, *rows]


def _read_jsonl(path: Path):
    records = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
            else:
                raise ValueError(f"JSONL line {line_no} is not an object")
    return records


def _completed_ids(ledger: Path, raw_dir: Path):
    completed = set()
    if not ledger.exists():
        return completed
    for row in _read_jsonl(ledger):
        if row.get("record_type") != "capture" or row.get("status") != "captured":
            continue
        pid, expected = row.get("late_post_id"), row.get("raw_sha256")
        raw_path = row.get("raw_response_path")
        if not isinstance(pid, str) or not isinstance(expected, str) or not isinstance(raw_path, str):
            continue
        if not _SAFE_POST_ID.fullmatch(pid) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            continue
        candidate = Path(raw_path).resolve()
        if candidate.parent != raw_dir.resolve() or candidate.name != f"{expected}.json":
            continue
        try:
            if candidate.stat().st_size > MAX_RESPONSE_BYTES:
                continue
            digest = hashlib.sha256()
            total = 0
            with candidate.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        break
                    digest.update(chunk)
            if total <= MAX_RESPONSE_BYTES and digest.hexdigest() == expected:
                body = json.loads(candidate.read_bytes())
                if _response_post_id(body) != pid:
                    continue
                completed.add(pid)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return completed


def _response_post_id(body):
    """Read the Zernio get_post envelope ID shape used by existing Echo readers."""
    if not isinstance(body, dict):
        return None
    post = body.get("post")
    if not isinstance(post, dict):
        return None
    value = post.get("_id") or post.get("id")
    return value if isinstance(value, str) and value else None


def capture_posts(records, *, ledger: Path, raw_dir: Path, max_items: int,
                  timeout: float, get_json, get_raw=None, now=None):
    """Capture exact IDs; injected get_json must enforce its GET-only deadline.

    The live CLI supplies _ProcessGet, which kills and reaps requests on timeout.
    """
    if max_items < 1 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("max_items and timeout must be positive")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    done = _completed_ids(ledger, raw_dir)
    ids = []
    for row in records:
        pid = row.get("late_post_id")
        if (row.get("record_type") == "row" and
                row.get("classification") == "exact_post_lookup_candidate" and
                isinstance(pid, str) and _SAFE_POST_ID.fullmatch(pid) and
                pid not in done and pid not in ids):
            ids.append(pid)
            if len(ids) == max_items:
                break
    timestamp = now or (lambda: datetime.now(timezone.utc).isoformat())
    outcomes = []
    for pid in ids:
        started = time.monotonic()
        try:
            body = get_json(pid)
            elapsed = time.monotonic() - started
            response_id = _response_post_id(body)
            if response_id != pid:
                raise ValueError("Zernio response post ID did not match requested ID")
            raw = get_raw() if get_raw else json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            digest = _sha256(raw)
            raw_path = raw_dir / f"{digest}.json"
            if not raw_path.exists() or _hash_file(raw_path) != digest:
                raw_path.write_bytes(raw)
            record = {"record_type": "capture", "late_post_id": pid,
                      "status": "captured", "captured_at": timestamp(),
                      "elapsed_seconds": round(elapsed, 6), "raw_sha256": digest,
                      "raw_response_path": str(raw_path),
                      "response_post_id": response_id,
                      "response_hash_basis": "exact_http_response_body" if get_raw else "canonical_json_body"}
        except Exception as exc:  # record bounded failure and allow resume/retry
            record = {"record_type": "capture", "late_post_id": pid,
                      "status": "error", "captured_at": timestamp(),
                      "error_type": getattr(exc, "error_type", type(exc).__name__),
                      "http_status": getattr(exc, "status", None)}
        with ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        outcomes.append(record)
        if record.get("error_type") == "TimeoutError":
            break
    return outcomes


class _RequestsGet:
    """GET-only adapter used by ZernioClient.get_post with an explicit timeout."""
    def __init__(self, timeout):
        self.timeout = timeout
        self.raw = None
        self._requests = None

    def get(self, url, *, params=None, headers=None, timeout=None):
        import requests
        self._requests = requests
        response = requests.get(url, params=params or {}, headers=headers or {},
                                timeout=self.timeout, stream=True)
        chunks = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ValueError("Zernio response exceeded 10 MiB capture limit")
                chunks.append(chunk)
        finally:
            response.close()
        self.raw = b"".join(chunks)
        response._content = self.raw
        response._content_consumed = True
        return response


def _fetch_post_worker(pid, timeout, output_dir):
    """Child owns the entire GET; only successful raw bytes or safe metadata leave it."""
    import contextlib
    import os
    root = Path(output_dir)
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            from agent import zernio
            adapter = _RequestsGet(timeout)
            client = zernio.ZernioClient(http=adapter)
            client.get_post(pid)
            (root / "response").write_bytes(adapter.raw)
            result = {"ok": True}
        except Exception as exc:
            status = getattr(exc, "status", None)
            result = {"ok": False, "error_type": type(exc).__name__,
                      "http_status": status if isinstance(status, int) else None}
        (root / "result.json").write_text(json.dumps(result), encoding="utf-8")


class _ProviderFailure(Exception):
    def __init__(self, error_type, status):
        super().__init__("Zernio capture failed")
        self.error_type = error_type
        self.status = status


class _ProcessGet:
    """Kill and reap a spawned GET worker before returning a wall deadline failure."""
    def __init__(self, timeout, worker=_fetch_post_worker):
        self.timeout = timeout
        self.worker = worker
        self.raw = None

    def __call__(self, pid):
        if not isinstance(pid, str) or not _SAFE_POST_ID.fullmatch(pid):
            raise ValueError("unsafe exact post ID")
        self.raw = None
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="zernio-history-") as output_dir:
            process = multiprocessing.get_context("spawn").Process(
                target=self.worker, args=(pid, self.timeout, output_dir))
            try:
                process.start()
                process.join(max(0, self.timeout - (time.monotonic() - started)))
                if process.is_alive():
                    # kill closes the child's transport, including a trickling response.
                    process.kill()
                    process.join()
                    raise TimeoutError("Zernio GET exceeded wall deadline")
                if process.exitcode != 0:
                    raise _ProviderFailure("WorkerError", None)
                root = Path(output_dir)
                result = _load_json(root / "result.json")
                if not result.get("ok"):
                    raise _ProviderFailure(result["error_type"], result.get("http_status"))
                raw_path = root / "response"
                if raw_path.stat().st_size > MAX_RESPONSE_BYTES:
                    raise ValueError("Zernio response exceeded 10 MiB capture limit")
                self.raw = raw_path.read_bytes()
                return json.loads(self.raw)
            finally:
                if process.pid is not None:
                    if process.is_alive():
                        process.kill()
                    process.join()
                process.close()


def _provider_getter(timeout):
    # Validate credentials without a request; the spawned worker owns the GET.
    from agent import zernio
    if not zernio.ZernioClient(http=_RequestsGet(timeout)).api_key:
        raise ValueError("ZERNIO_API_KEY is not set")
    getter = _ProcessGet(timeout)
    return getter, lambda: getter.raw


def _hash_file(path):
    if path.stat().st_size > MAX_RESPONSE_BYTES:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    offline = sub.add_parser("classify", help="verify indexed snapshot pages and exact IDs offline")
    offline.add_argument("--index", type=Path, required=True)
    offline.add_argument("--evidence-root", type=Path, required=True)
    offline.add_argument("--output", type=Path, required=True)
    capture = sub.add_parser("capture", help="opt-in bounded read-only Zernio GET capture")
    capture.add_argument("--input", type=Path, required=True, help="classifier JSONL")
    capture.add_argument("--ledger", type=Path, required=True)
    capture.add_argument("--raw-dir", type=Path, required=True)
    capture.add_argument("--max-items", type=int, required=True)
    capture.add_argument("--timeout", type=float, required=True, help="per-request seconds")
    capture.add_argument("--capture-zernio", action="store_true", required=True,
                         help="required explicit opt-in; performs GET only")
    args = parser.parse_args(argv)
    if args.command == "classify":
        _write_jsonl(args.output, classify_index(args.index, args.evidence_root))
        return 0
    records = _read_jsonl(args.input)
    get_json, get_raw = _provider_getter(args.timeout)
    results = capture_posts(records, ledger=args.ledger, raw_dir=args.raw_dir,
                            max_items=args.max_items, timeout=args.timeout,
                            get_json=get_json, get_raw=get_raw)
    print(json.dumps({"attempted": len(results), "captured": sum(r["status"] == "captured" for r in results),
                      "errors": sum(r["status"] == "error" for r in results),
                      "ledger": str(args.ledger), "raw_dir": str(args.raw_dir)}, sort_keys=True))
    return 0 if all(r["status"] == "captured" for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
