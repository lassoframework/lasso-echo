"""Read-only Swift River Drive reachability preflight. Run with Railway env.

  railway run python3 -m scripts.preflight_swift_drive_sources

Prints only source IDs and counts; never indexes, downloads, or writes. Do not
run during deploy: this is an operator preflight with Drive SA access.
"""
from agent.integrations.drive_client import DriveClient
from agent.media_source_store import SupabaseMediaStore
from agent.config import gym_drive_sync_max_depth

GYM = "swiftrivercrossfite5c9db"
SOURCE_IDS = ("2f7363015426445d981b39ba17857cb4",
              "8eef465c3b2444d2be3d6eab5c6d8bc5")


def main():
    store, drive = SupabaseMediaStore(), DriveClient()
    if not store.available() or not drive.available():
        raise SystemExit("preflight unavailable: media store or Drive credentials missing")
    sources = {s["id"]: s for s in store.list_sources(GYM)}
    counts = []
    for source_id in SOURCE_IDS:
        source = sources.get(source_id)
        if (not source or source.get("kind") != "gym_drive"
                or source.get("active") is not True):
            raise SystemExit(f"{source_id}: active gym Drive source missing")
        try:
            files = drive.walk(source["folder_id"], max_depth=gym_drive_sync_max_depth(),
                               use_cache=False)
        except Exception as exc:
            raise SystemExit(f"{source_id}: Drive walk failed ({type(exc).__name__})")
        media_ids = {f.id for f in files if f.mime_type.startswith(("image/", "video/"))}
        counts.append(media_ids)
        print(f"{source_id}: {len(media_ids)} reachable media files")
    print(f"overlapping Drive file count: {len(counts[0] & counts[1])}")


if __name__ == "__main__":
    main()
