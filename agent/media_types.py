"""
media_types.py — THE ONE definition of "is this url/path a video" (audit D1, 2026-09-10).

Seven modules each carried their own video extension tuple and they had drifted:
gym_media_index admitted .avi/.mkv/.hevc as eligible Drive videos, zernio typed
mediaItems by a tuple missing .mkv/.hevc (so such a file would have been sent as
type "image"), calendar_autopublish's aspect preflight knew only .mp4/.mov/.webm,
portal_social / media_swap / client_month_run / gbp_mirror knew four. One constant,
imported everywhere, so the answer cannot disagree between the lane that stages a
file and the wire that publishes it.

Two tiers, on purpose:
  VIDEO_EXTS              everything Echo RECOGNISES as video (index, display, publish
                          typing). A file with one of these extensions is never treated
                          as an image anywhere.
  PUBLISHABLE_VIDEO_EXTS  the containers Zernio -> Instagram/Facebook actually carry
                          (.mp4 / .mov / .m4v). Anything else must be transcoded to an
                          H.264 .mp4 rendition (gym_media_index.ensure_rendition) BEFORE
                          it is staged, or be skipped with a reason. .webm / .avi / .mkv
                          / .hevc are recognised, never shipped raw.
"""

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".hevc")
PUBLISHABLE_VIDEO_EXTS = (".mp4", ".mov", ".m4v")


def ext_of(url_or_path) -> str:
    """The lower-cased extension of a url or path, query string stripped ('' when none)."""
    s = str(url_or_path or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    dot = s.rfind(".")
    slash = s.rfind("/")
    if dot < 0 or dot < slash:
        return ""
    return s[dot:].lower()


def is_video_url(url_or_path) -> bool:
    """True when the url/path ends in ANY recognised video extension."""
    return ext_of(url_or_path) in VIDEO_EXTS


def is_publishable_video(url_or_path) -> bool:
    """True when the url/path is a video in a container Zernio -> IG/FB can carry."""
    return ext_of(url_or_path) in PUBLISHABLE_VIDEO_EXTS


__all__ = ["VIDEO_EXTS", "PUBLISHABLE_VIDEO_EXTS", "ext_of", "is_video_url",
           "is_publishable_video"]
