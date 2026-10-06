import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import burst_spacing  # noqa: E402
from agent import client_content, config, rotation  # noqa: E402
from agent.library import Creative  # noqa: E402


def _photo(tmp_path, batch, number):
    path = tmp_path / f"{batch}_IMG_{number:04d}.jpg"
    path.write_bytes(b"photo" + str(number).encode())
    return Creative(path=str(path), media_type="image")


def test_multiple_camera_bursts_are_maximally_spaced(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BURST_SPACING_GYMS", "nine7")
    photos = [
        _photo(tmp_path, "20261001T120000Z", 1001),
        _photo(tmp_path, "20261001T120000Z", 1002),
        _photo(tmp_path, "20261002T120000Z", 2001),
        _photo(tmp_path, "20261002T120000Z", 2002),
        _photo(tmp_path, "20261003T120000Z", 3001),
        _photo(tmp_path, "20261003T120000Z", 3002),
    ]
    served = {"nine7_ig": []}
    chosen_batches = []
    remaining = list(photos)
    for day in ("2026-10-10", "2026-10-11", "2026-10-12"):
        pool = burst_spacing.choose_spaced_pool(
            remaining, photos, served, "nine7_ig", day)
        chosen = sorted(pool, key=lambda creative: creative.path)[0]
        chosen_batches.append(os.path.basename(chosen.path).split("_IMG_")[0])
        served["nine7_ig"].append({
            "key": os.path.basename(chosen.path), "date": day,
        })
        remaining.remove(chosen)

    assert len(set(chosen_batches)) == 3


def test_sequence_gap_splits_two_bursts_inside_one_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BURST_SPACING_GYMS", "nine7")
    photos = [
        _photo(tmp_path, "20261001T120000Z", 1001),
        _photo(tmp_path, "20261001T120000Z", 1002),
        _photo(tmp_path, "20261001T120000Z", 1050),
        _photo(tmp_path, "20261001T120000Z", 1051),
    ]
    cohorts = burst_spacing.cohort_map(photos)
    assert cohorts[photos[0].path] == cohorts[photos[1].path]
    assert cohorts[photos[2].path] == cohorts[photos[3].path]
    assert cohorts[photos[0].path] != cohorts[photos[2].path]


def test_unknown_or_thin_library_preserves_legacy_order(tmp_path):
    unknown = []
    for name in ("team.jpg", "coach.jpg"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        unknown.append(Creative(path=str(path), media_type="image"))
    assert burst_spacing.choose_spaced_pool(
        unknown, unknown, {}, "nine7_ig", "2026-10-10") == unknown
    assert burst_spacing.choose_spaced_pool(
        unknown[:1], unknown[:1], {}, "nine7_ig", "2026-10-10") == unknown[:1]


def test_compact_camera_sequence_parser_preserves_rollover_family():
    assert burst_spacing.parse_camera_sequence("DSCN0999.JPG") == ("dscn", 999)
    assert burst_spacing.parse_camera_sequence("DSCN1000.JPG") == ("dscn", 1000)
    assert burst_spacing.parse_camera_sequence("IMG1001.jpg") == ("img", 1001)
    assert burst_spacing.parse_camera_sequence("IMG_1002.jpg") == ("img", 1002)


def test_compact_camera_rollover_stays_in_one_burst_cohort(tmp_path):
    photos = []
    for sequence in ("0999", "1000"):
        path = tmp_path / f"20261001T120000Z_DSCN{sequence}.jpg"
        path.write_bytes(sequence.encode())
        photos.append(Creative(path=str(path), media_type="image"))

    cohorts = burst_spacing.cohort_map(photos)

    assert cohorts[photos[0].path] == cohorts[photos[1].path]


def test_burst_spacing_is_default_off_and_explicitly_scoped(tmp_path, monkeypatch):
    photos = [
        _photo(tmp_path, "20261001T120000Z", 1001),
        _photo(tmp_path, "20261002T120000Z", 2001),
    ]
    monkeypatch.delenv("AGENT_BURST_SPACING_GYMS", raising=False)
    assert config.burst_spacing_gyms() == set()
    assert burst_spacing.choose_spaced_pool(
        photos, photos, {}, "nine7_ig", "2026-10-10") == photos

    monkeypatch.setenv("AGENT_BURST_SPACING_GYMS", " NINE7 , othergym ")
    assert config.burst_spacing_enabled_for("nine7_fb") is True
    assert config.burst_spacing_enabled_for("unlisted_ig") is False
    assert len(burst_spacing.choose_spaced_pool(
        photos, photos, {}, "nine7_ig", "2026-10-10")) == 1


def test_legacy_picker_spaces_bursts_with_vision_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BURST_SPACING_GYMS", "nine7")
    photos = [
        _photo(tmp_path, "20261001T120000Z", 1001),
        _photo(tmp_path, "20261001T120000Z", 1002),
        _photo(tmp_path, "20261002T120000Z", 2001),
        _photo(tmp_path, "20261002T120000Z", 2002),
    ]
    served = {"nine7_ig": []}
    monkeypatch.setattr(config, "vision_enabled_for", lambda _account: False)
    monkeypatch.setattr(rotation, "load_served_strict", lambda: served)
    monkeypatch.setattr(rotation, "load_served", lambda: served)
    monkeypatch.setattr(
        rotation, "globally_available_local_paths",
        lambda _account, paths: set(paths),
    )
    from agent import media_bridge
    monkeypatch.setattr(media_bridge, "enabled", lambda: False)

    first = client_content.pick_image(
        "nine7_ig", "2026-10-10", str(tmp_path), prefer_photos=True)
    served["nine7_ig"].append({
        "key": os.path.basename(first.path), "date": "2026-10-10",
    })
    second = client_content.pick_image(
        "nine7_ig", "2026-10-11", str(tmp_path), prefer_photos=True)

    assert first is not None and second is not None
    assert os.path.basename(first.path).split("_IMG_")[0] != (
        os.path.basename(second.path).split("_IMG_")[0])


def test_real_picker_preserves_unknown_asset_in_mixed_cohort_library(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BURST_SPACING_GYMS", "nine7")
    _photo(tmp_path, "20261001T120000Z", 1001)
    _photo(tmp_path, "20261002T120000Z", 2001)
    legacy = tmp_path / "000_legacy_team.jpg"
    legacy.write_bytes(b"legacy-photo")
    served = {"nine7_ig": []}
    monkeypatch.setattr(config, "vision_enabled_for", lambda _account: False)
    monkeypatch.setattr(rotation, "load_served_strict", lambda: served)
    monkeypatch.setattr(rotation, "load_served", lambda: served)
    monkeypatch.setattr(
        rotation, "globally_available_local_paths",
        lambda _account, paths: set(paths),
    )
    from agent import media_bridge
    monkeypatch.setattr(media_bridge, "enabled", lambda: False)

    picked = client_content.pick_image(
        "nine7_ig", "2026-10-10", str(tmp_path), prefer_photos=True)

    assert picked is not None
    assert picked.path == str(legacy)


def test_cross_cohort_dupe_group_history_is_conservative(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BURST_SPACING_GYMS", "nine7")
    first = _photo(tmp_path, "20261001T120000Z", 1001)
    second = _photo(tmp_path, "20261002T120000Z", 2001)
    third = _photo(tmp_path, "20261003T120000Z", 3001)
    from agent import dam
    dam.write_sidecar(first.path, {"dupe_group": "shared-burst"})
    dam.write_sidecar(second.path, {"dupe_group": "shared-burst"})
    served = {"nine7_ig": [{"key": "shared-burst", "date": "2026-10-09"}]}

    chosen = burst_spacing.choose_spaced_pool(
        [first, second, third], [first, second, third], served,
        "nine7_ig", "2026-10-10")

    assert chosen == [third]


def test_preexisting_library_backfill_activates_complete_cohort_spacing(
        tmp_path, monkeypatch):
    from agent import client_media_sync as cms

    monkeypatch.setenv("AGENT_BURST_SPACING_GYMS", "nine7")
    names = [
        "20261001T120000Z_IMG_1001.jpg",
        "20261001T120000Z_IMG_1002.jpg",
        "20261002T120000Z_IMG_2001.jpg",
        "20261002T120000Z_IMG_2002.jpg",
    ]
    objects = {
        f"intake/nine7/incoming/{name}": b"remote" for name in names
    }
    for stamp in ("20261001T120000Z", "20261002T120000Z"):
        batch_names = [name for name in names if name.startswith(stamp)]
        objects[f"intake/nine7/incoming/{stamp}_upload.json"] = json.dumps({
            "timestamp": stamp, "filenames": batch_names,
        }).encode()

    class R2:
        def __init__(self):
            self.got = []

        def list_keys(self, prefix):
            return [key for key in objects if key.startswith(prefix)]

        def get_bytes(self, key):
            self.got.append(key)
            return objects[key]

    r2 = R2()
    for name in names:
        (tmp_path / name).write_bytes(b"already-local")

    assert cms.sync_uploads("nine7", r2=r2, out_dir=str(tmp_path)) == {
        "synced": 0, "skipped": 4,
    }
    assert not [key for key in r2.got if key.endswith(".jpg")]
    creatives = [Creative(path=str(tmp_path / name), media_type="image")
                 for name in names]
    cohorts = burst_spacing.cohort_map(creatives)
    assert len(cohorts) == 4
    assert len(set(cohorts.values())) == 2
    spaced = burst_spacing.choose_spaced_pool(
        creatives, creatives, {}, "nine7_ig", "2026-10-10")
    assert 0 < len(spaced) < len(creatives)


def test_unchanged_cohort_map_is_cached_and_invalidates_on_sidecar_edit(
        tmp_path, monkeypatch):
    photo = _photo(tmp_path, "20261001T120000Z", 1001)
    from agent import dam
    real_read = dam.read_sidecar
    reads = []

    def counted(path):
        reads.append(path)
        return real_read(path)

    monkeypatch.setattr(dam, "read_sidecar", counted)
    first = burst_spacing.cohort_map([photo])
    hits_before = burst_spacing._cohort_items.cache_info().hits
    second = burst_spacing.cohort_map([photo])
    assert first == second
    assert reads == [photo.path]
    assert burst_spacing._cohort_items.cache_info().hits == hits_before + 1

    with open(dam.sidecar_path(photo.path), "w", encoding="utf-8") as fh:
        json.dump({"intake_batch_position": 7,
                   "cache_invalidator": "different-size"}, fh)
    assert burst_spacing.cohort_map([photo]) == first
    assert reads == [photo.path, photo.path]


def test_batch_position_is_deterministic_within_selected_cohort(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BURST_SPACING_GYMS", "nine7")
    first_path = tmp_path / "20261001T120000Z_IMG_1001_z.jpg"
    second_path = tmp_path / "20261001T120000Z_IMG_1001_a.jpg"
    other_path = tmp_path / "20261002T120000Z_IMG_2001.jpg"
    photos = [
        Creative(path=str(first_path), media_type="image"),
        Creative(path=str(second_path), media_type="image"),
        Creative(path=str(other_path), media_type="image"),
    ]
    from agent import dam
    for path in (first_path, second_path, other_path):
        path.write_bytes(b"photo")
    common = {"intake_batch_timestamp": "20261001T120000Z",
              "intake_camera_family": "img", "intake_camera_sequence": 1001}
    dam.write_sidecar(str(first_path), {**common, "intake_batch_position": 0})
    dam.write_sidecar(str(second_path), {**common, "intake_batch_position": 1})
    dam.write_sidecar(str(other_path), {
        "intake_batch_timestamp": "20261002T120000Z",
        "intake_batch_position": 0,
        "intake_camera_family": "img", "intake_camera_sequence": 2001,
    })

    served = {"nine7_ig": [{
        "key": dam.rotation_key(str(other_path)), "date": "2026-10-09",
    }]}
    chosen = burst_spacing.choose_spaced_pool(
        photos, photos, served, "nine7_ig", "2026-10-10")

    assert chosen[:2] == photos[:2]
