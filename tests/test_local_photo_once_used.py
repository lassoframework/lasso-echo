"""
Durable once-used guard for client-uploaded LOCAL photos (Blake, 2026-10-02).

A local photo that was ever planned/served for a gym is ineligible forever,
including on the same date: account/date alone cannot prove two rows are one
mirrored post. The rule crosses the 14-day window, the vision §3 windows, the old
record_served prune cutoff, and the allow_reuse=True last-resort pass. The guard
fails closed: an unreadable served ledger means no local pick. Offline (tmp sqlite
and tmp library).
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content, dam, rotation  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))   # fresh served ledger
    monkeypatch.delenv("AGENT_VISION_GYMS", raising=False)           # legacy branch
    yield


def _lib(tmp_path, names):
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    for n in names:
        (lib / n).write_bytes(b"\xff\xd8\xffFAKEJPEG" + n.encode())
    return str(lib)


def _serve(lib, name, account_key, day_key):
    rotation.record_served(account_key, dam.rotation_key(os.path.join(lib, name)),
                           "service", day_key)


# ---- 1. a served local photo is ineligible on every later date ----------------
def test_served_photo_never_picked_again(tmp_path):
    lib = _lib(tmp_path, ["a.jpg", "b.jpg"])
    _serve(lib, "a.jpg", "gymx_ig", "2026-09-01")
    for day in ("2026-09-02", "2026-09-20", "2026-12-25"):
        pick = client_content.pick_image("gymx_ig", day, lib)
        assert pick is not None and os.path.basename(pick.path) == "b.jpg", day


def test_whole_library_served_returns_none(tmp_path):
    lib = _lib(tmp_path, ["only.jpg"])
    _serve(lib, "only.jpg", "gymx_ig", "2026-09-01")
    assert client_content.pick_image("gymx_ig", "2026-09-02", lib) is None


# ---- 2. durability: the old prune cutoff no longer resurrects a photo --------
def test_record_served_keeps_local_rows_beyond_prune_cutoff(tmp_path):
    lib = _lib(tmp_path, ["old.jpg"])
    _serve(lib, "old.jpg", "gymx_ig", "2025-01-01")      # far beyond any window
    # a later serve triggers the prune pass; generated rows from the same old
    # date ARE pruned, the local photo row must survive.
    rotation.record_served("gymx_ig", "nano:stale", "brain:p1", "2025-01-01")
    _serve(lib, "old.jpg", "gymx_ig", "2026-10-02")      # triggers prune
    entries = rotation.load_served().get("gymx_ig", [])
    keys = {e["key"] for e in entries}
    assert "old.jpg" in keys, "local photo history was pruned"
    assert "nano:stale" not in keys, "generated rows should still prune"
    # ...and the photo stays ineligible even though it 'aged out' long ago
    assert client_content.pick_image("gymx_ig", "2026-10-03", lib) is None


# ---- 3. allow_reuse fallback is not a route back ------------------------------
def test_allow_reuse_does_not_resurrect(tmp_path):
    lib = _lib(tmp_path, ["only.jpg"])
    _serve(lib, "only.jpg", "gymx_ig", "2026-09-20")
    pick = client_content.pick_image("gymx_ig", "2026-10-02", lib, allow_reuse=True)
    assert pick is None


# ---- 4. same-day reuse is blocked without explicit same-post proof ------------
def test_same_day_platform_sibling_without_post_proof_is_blocked(tmp_path):
    lib = _lib(tmp_path, ["shot.jpg"])
    _serve(lib, "shot.jpg", "gymx_ig", "2026-10-02")     # IG placed it today
    assert client_content.pick_image("gymx_fb", "2026-10-02", lib) is None


def test_same_day_gbp_is_not_a_platform_sibling(tmp_path):
    lib = _lib(tmp_path, ["shot.jpg"])
    _serve(lib, "shot.jpg", "gymx_ig", "2026-10-02")
    assert client_content.pick_image("gymx_gbp", "2026-10-02", lib) is None


def test_same_day_unknown_lane_is_not_a_platform_sibling(tmp_path):
    lib = _lib(tmp_path, ["shot.jpg"])
    _serve(lib, "shot.jpg", "gymx_ig", "2026-10-02")
    assert client_content.pick_image("gymx", "2026-10-02", lib) is None


def test_record_served_reports_write_failure(monkeypatch):
    monkeypatch.setattr(rotation, "_conn",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert rotation.record_served("gymx_ig", "shot.jpg", "service",
                                  "2026-10-02") is False


def test_release_served_deletes_only_its_exact_reservation():
    first = rotation.reserve_served("gymx_ig", "shot.jpg", "service", "2026-10-02")
    second = rotation.reserve_served("gymx_ig", "shot.jpg", "service", "2026-10-02")
    assert first and second and first != second

    assert rotation.release_served(second) is True
    entries = rotation.load_served_strict()["gymx_ig"]
    assert len(entries) == 1 and entries[0]["key"] == "shot.jpg"


def test_concurrent_video_aliases_have_one_winner(tmp_path):
    a = tmp_path / "first.mp4"
    b = tmp_path / "alias.mp4"
    a.write_bytes(b"same video bytes")
    b.write_bytes(a.read_bytes())
    gate = Barrier(2)

    def claim(item):
        lane, path = item
        gate.wait()
        return rotation.reserve_local_media_once(
            f"gymx_{lane}", path.name, "video", "2026-10-03", path=str(path))

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(claim, (("ig", a), ("gbp", b))))
    assert sum(value is not None for value in ids) == 1
    assert rotation.reserve_local_media_once(
        "other_ig", b.name, "video", "2026-10-03", path=str(b))


def test_same_day_same_lane_blocked(tmp_path):
    lib = _lib(tmp_path, ["shot.jpg"])
    _serve(lib, "shot.jpg", "gymx_ig", "2026-10-02")
    assert client_content.pick_image("gymx_ig", "2026-10-02", lib) is None


def test_sibling_serve_on_another_day_blocks(tmp_path):
    lib = _lib(tmp_path, ["shot.jpg"])
    _serve(lib, "shot.jpg", "gymx_ig", "2026-10-01")     # IG used it YESTERDAY
    assert client_content.pick_image("gymx_fb", "2026-10-02", lib) is None


# ---- 5. a different gym's identical basename is a different photo -------------
def test_other_gyms_same_basename_never_blocks(tmp_path):
    lib = _lib(tmp_path, ["photo.jpg"])
    _serve(lib, "photo.jpg", "someone_else_ig", "2026-09-01")
    pick = client_content.pick_image("gymx_ig", "2026-09-02", lib)
    assert pick is not None and os.path.basename(pick.path) == "photo.jpg"


# ---- 6. near-dupe cluster: serving one member consumes the whole group --------
def test_dupe_group_member_consumes_cluster(tmp_path):
    lib = _lib(tmp_path, ["m1.jpg", "m2.jpg"])
    import json
    for m in ("m1", "m2"):
        with open(os.path.join(lib, m + ".json"), "w") as fh:
            json.dump({"dupe_group": "burst1"}, fh)
    _serve(lib, "m1.jpg", "gymx_ig", "2026-09-01")
    assert client_content.pick_image("gymx_ig", "2026-09-02", lib) is None


# ---- 7. fail closed: an unreadable ledger means no local pick -----------------
def test_unreadable_ledger_fails_closed(tmp_path, monkeypatch):
    lib = _lib(tmp_path, ["fresh.jpg"])

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(rotation, "load_served_strict", _boom)
    assert client_content.pick_image("gymx_ig", "2026-10-02", lib) is None


def test_guard_unit_fails_closed_on_read_error(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(rotation, "load_served_strict", _boom)
    assert rotation.local_photo_served("fresh.jpg", "gymx_ig", "2026-10-02") is True


def test_empty_cluster_key_fails_closed():
    assert rotation.local_photo_served("", "gymx_ig", "2026-10-02",
                                       served={"gymx_ig": []}) is True
