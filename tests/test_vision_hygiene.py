"""
P2 — library hygiene (ECHO_VISION_SPEC §3): Hamming-<=6 near-dupe clustering, the
cluster-count starvation input, and the per-platform reuse windows. Offline: sidecars carry
explicit pHashes so clustering is exercised without image generation.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import vision, rotation  # noqa: E402


def _asset(lib, name, phash):
    """A dummy image file + a sidecar carrying a stored analysis with this pHash."""
    open(os.path.join(lib, name), "wb").close()
    stem = os.path.splitext(name)[0]
    with open(os.path.join(lib, stem + ".json"), "w") as fh:
        json.dump({"media_analysis": {"version": 2, "phash": phash}}, fh)


# ---- clustering ------------------------------------------------------------------------

def test_cluster_library_groups_near_dupes(tmp_path):
    lib = str(tmp_path)
    _asset(lib, "a.png", "0000000000000000")
    _asset(lib, "b.png", "0000000000000003")   # Hamming 2 from a -> same cluster
    _asset(lib, "c.png", "ffffffffffffffff")   # Hamming 64 -> its own
    groups = vision.cluster_library(lib)
    assert "a.png" in groups and set(groups["a.png"]) == {"a.png", "b.png"}
    assert "c.png" not in groups                 # singleton, no dupe_group
    # rotation keys collapse: a and b share the group, c stands alone
    from agent import dam
    assert dam.rotation_key(os.path.join(lib, "b.png")) == "a.png"
    assert dam.rotation_key(os.path.join(lib, "c.png")) == "c.png"


def test_cluster_count_counts_clusters_not_images(tmp_path):
    lib = str(tmp_path)
    _asset(lib, "a.png", "0000000000000000")
    _asset(lib, "b.png", "0000000000000001")   # near-dupe of a
    _asset(lib, "c.png", "00000000000000ff")   # near-dupe of a (Hamming 8? -> check)
    _asset(lib, "d.png", "ffffffffffffffff")   # different
    vision.cluster_library(lib)
    # a+b cluster; c is Hamming 8 from a (>6) so it's its own; d different.
    # => 3 clusters (ab, c, d), not 4 images
    assert vision.cluster_count(lib) == 3


# ---- per-platform reuse windows --------------------------------------------------------

def _served(*entries):
    """entries: (account_key, key, date). Build the load_served() shape."""
    out = {}
    for acct, key, date in entries:
        out.setdefault(acct, []).append({"key": key, "pillar": "", "date": date,
                                         "archetype": "", "set": ""})
    return out


def test_ig_reuse_blocked_within_60_days():
    s = _served(("gritx_ig", "clusterA", "2026-08-01"))
    # 40 days later on IG -> blocked (within 60)
    assert rotation.reuse_blocked("clusterA", "gritx_ig", "2026-09-10", served=s) is True
    # 70 days later -> allowed
    assert rotation.reuse_blocked("clusterA", "gritx_ig", "2026-10-11", served=s) is False


def test_gbp_may_reuse_ig_image_after_14_days():
    s = _served(("gritx_ig", "clusterA", "2026-09-01"))
    # GBP, 10 days after the IG serve -> blocked (cross-surface 14d window)
    assert rotation.reuse_blocked("clusterA", "googlebusiness", "2026-09-11", served=s) is True
    # GBP, 20 days after -> allowed
    assert rotation.reuse_blocked("clusterA", "googlebusiness", "2026-09-21", served=s) is False


def test_gbp_not_twice_in_a_month():
    s = _served(("googlebusiness", "clusterA", "2026-09-01"))
    assert rotation.reuse_blocked("clusterA", "googlebusiness", "2026-09-20", served=s) is True
    assert rotation.reuse_blocked("clusterA", "googlebusiness", "2026-10-05", served=s) is False


def test_reuse_unknown_cluster_never_blocks():
    assert rotation.reuse_blocked("neverseen", "gritx_ig", "2026-09-10", served={}) is False
    assert rotation.reuse_blocked("", "gritx_ig", "2026-09-10") is False


def test_fb_mirror_shares_ig_window():
    s = _served(("gritx_fb", "clusterA", "2026-09-01"))
    # an IG target sees the FB serve within the 60d window (feed mirror = same asset)
    assert rotation.reuse_blocked("clusterA", "gritx_ig", "2026-09-15", served=s) is True
