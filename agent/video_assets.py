"""
Video editor brand assets + helpers (persistent, reproducible).

All assets live in agent/assets/ (committed to the repo, so renders are
reproducible on any checkout / the persistent volume):
  brand/lasso_mark.png                    the red L mark, white/navy knocked out
  fonts/Anton-Regular.ttf                 caption font (Word Highlight)
  fonts/Oswald-Medium.ttf                 handle font (Minimal Broadcast)
  brand/haarcascade_frontalface_default.xml  face detector fallback

Face detection (caption placement) is OPTIONAL: if opencv is importable it is
used to keep captions off the speaker's face; otherwise a fixed lower-third
band is used. Headless environments without opencv still render fine.
"""

import os

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
BRAND_DIR = os.path.join(ASSETS_DIR, "brand")
FONTS_DIR = os.path.join(ASSETS_DIR, "fonts")


def lasso_mark_path():
    """Red L mark on transparent. Raises if missing (asset is required)."""
    p = os.path.join(BRAND_DIR, "lasso_mark.png")
    if not os.path.isfile(p):
        raise FileNotFoundError(
            f"LASSO mark asset missing at {p}. Regenerate it from the brand logo.")
    return p


def anton_font_path():
    return os.path.join(FONTS_DIR, "Anton-Regular.ttf")


def oswald_font_path():
    p = os.path.join(FONTS_DIR, "Oswald-Medium.ttf")
    return p if os.path.isfile(p) else os.path.join(FONTS_DIR, "Oswald-SemiBold.ttf")


def _face_cascade_path():
    """opencv bundled cascade first, repo fallback second."""
    try:
        import cv2
        p = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        if os.path.isfile(p):
            return p
    except Exception:
        pass
    repo = os.path.join(BRAND_DIR, "haarcascade_frontalface_default.xml")
    return repo if os.path.isfile(repo) else None


def detect_face_bottom_frac(video_path, samples=8):
    """
    Sample frames and return the normalized y (0..1 from top) of the BOTTOM of the
    largest detected face across the clip, so captions can be placed safely below
    it. Returns None when opencv is unavailable, no face is found, or on any error
    (caller then uses a fixed lower-third band). Never raises.
    """
    try:
        import cv2
    except Exception:
        return None
    cascade_path = _face_cascade_path()
    if not cascade_path:
        return None
    try:
        cascade = cv2.CascadeClassifier(cascade_path)
        if cascade.empty():
            return None
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            cap.release()
            return None
        max_bottom = 0.0
        found = False
        for i in range(samples):
            frame_no = int(total * (i + 0.5) / samples)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            h = frame.shape[0]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                             minSize=(int(frame.shape[1] * 0.08),
                                                      int(h * 0.08)))
            for (fx, fy, fw, fh) in faces:
                found = True
                bottom = (fy + fh) / float(h)
                if bottom > max_bottom:
                    max_bottom = bottom
        cap.release()
        if not found:
            return None
        return min(0.95, max_bottom)
    except Exception:
        return None
