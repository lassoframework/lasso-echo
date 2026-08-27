> Delivered by Blake 2026-08-27. Internal spec/reference — NOT approved caption source material. Never cite these numbers in client-facing or LASSO-facing captions.

# CC BUILD — Echo Podcast Library Ingest (`podcast_library`)

**Goal:** give Echo standing read access to the LASSO podcast Drive library so it can pull any episode clip at any time, and fill the LASSO calendar from real video instead of text cards.
**Fixes:** the frequency problem, the 0-humans-in-84-posts problem, and the 4x caption repeat — all three come from having no video source wired in.
**Publisher:** Zernio. Everything staged lands `pending`. **The human tap does not change.**

---

## 0. What is actually in the Drive folder (verified 2026-08-27)

Root: `Podcast Episodes` — folder id `1hfkXefD7kwOWkNIHSc0jOHLkUFbrh-C6`
Owner: `info@vasforgyms.com`. Parent: `1dWxVbdvU49957Z1KUMrbgppd1kLtRo_7`.

**~100 episode subfolders**, titled by bare episode number: `42` … `141`.

### Per-episode contents (episode 140, the best-formed example)
```
140/
├── GMMS 140                              Google Doc, ~5 KB   <- show notes / transcript
├── 140-Audio.mp3                         50 MB
├── 140-Video.mp4                         1.59 GB             <- full episode, NOT postable
└── Promo (Canva, Reels, Audiogram)/
    ├── GMMS-140-S1.mp4                   248 MB              <- postable clip
    ├── GMMS-140-S2.mp4                   174 MB
    ├── GMMS-140-S3.mp4                   162 MB
    ├── GMMS-140-S4.mp4                   139 MB
    └── 140-audiogram.mp4                 12 MB
```

### Confirmed clip inventory
Four short clips (S1–S4) exist for episodes **125–141**: 125, 126, 127, 128, 129, 130, 131, 132, 134, 135, 136, 137, 138, 139, 140.
**That is ~60 ready-to-post clips already sitting in Drive, unused.** At 4 posts/week that is **15 weeks of video** before anything new has to be produced.

Older episodes (42–124) generally carry only the full episode video. Clip sizes range 30 MB – 268 MB; full episodes 118 MB – 1.88 GB.

### ⚠️ Naming is inconsistent — do not build a path-based resolver
Clip names observed:
| Pattern | Episodes |
|---|---|
| `GMMS-{ep}-S{n}.mp4` | 129, 130, 131, 132, 134, 135, 137, 138, 140 |
| `GMMS-EP{ep}-S{n}.mp4` | 125, 126, 127, 128, 139 |
| `GMMS{ep}-S{n}.mp4` | 136 |

Full-episode names observed: `141-Video.mp4`, `140-Video.mp4`, `GMMS-104-V1.mp4`, `108-GMMS-V1.mp4`, `GMMS-VIDEO-98.mp4`, `86_GMMS.mp4`, `GMMS_82.mp4`, `GMMS-92.mp4`, `GMMS-61-V2.mp4`, `113-GMMS-V1.mp4`, `GMMS-103-VIDEO-V1.mp4`, `GMMS_121_V1.mp4`. **At least 12 conventions.**

The promo subfolder title also varies. Resolve by **regex over the recursive file listing**, keyed on the episode number from the parent folder title. Never hardcode a path.

---

## WAVE 1 — Drive access (`agent/integrations/drive_client.py`)

### 1.1 Auth — standing access, not a one-off token
Create a **Google Cloud service account** for Echo (`echo-media-reader@<project>.iam.gserviceaccount.com`) with scope `https://www.googleapis.com/auth/drive.readonly`.

Then **Blake shares the `Podcast Episodes` folder to that service-account email as Viewer.** That is the whole grant. It survives forever, needs no OAuth refresh dance, and is revocable by un-sharing one folder.

Key material goes in the existing secrets store as `GOOGLE_DRIVE_SA_JSON`. **Never commit it, never put it in the repo, never log it.**

Read-only scope is deliberate: Echo must not be able to modify or delete the podcast library.

### 1.2 Client surface
```python
# drive_client.py
def list_children(folder_id: str) -> list[DriveFile]      # one level
def walk(folder_id: str, max_depth: int = 3) -> list[DriveFile]
def download(file_id: str, dest: Path) -> Path            # streaming, resumable
def export_doc_text(file_id: str) -> str                  # Google Doc -> plain text
```
`DriveFile` carries `id, title, mime_type, size_bytes, parent_id, modified_time`.

Rate limits: Drive allows ~1,000 req/100s/user. Cache the folder tree for 6 hours in the existing kv store; a cold walk of 100 episode folders is ~110 requests.

---

## WAVE 2 — Episode index (`agent/podcast_index.py`)

### 2.1 Schema
```sql
CREATE TABLE podcast_asset (
  id              text PRIMARY KEY,      -- drive file id
  episode         int  NOT NULL,         -- from parent folder title
  kind            text NOT NULL,         -- 'clip' | 'audiogram' | 'full_video' | 'audio' | 'notes'
  clip_index      int,                   -- 1..4 for clips, null otherwise
  title           text NOT NULL,
  size_bytes      bigint,
  duration_sec    numeric,               -- probed on first download, null until then
  width           int,
  height          int,
  aspect          text,                  -- '9:16' | '1:1' | '16:9' | 'other'
  postable        boolean,               -- computed, see 2.3
  reject_reason   text,
  used_count      int  NOT NULL DEFAULT 0,
  last_used_at    timestamptz,
  notes_doc_id    text,                  -- the episode's Google Doc
  indexed_at      timestamptz NOT NULL
);
CREATE INDEX ON podcast_asset (postable, used_count, last_used_at);
```

### 2.2 Classifier
```python
EP_FROM_FOLDER  = re.compile(r'^\s*(\d{2,3})\s*$')
CLIP_RE = re.compile(r'^GMMS[-_ ]?(?:EP)?(\d{2,3})[-_ ]?S(\d)\.mp4$', re.I)
AUDIOGRAM_RE = re.compile(r'audiogram', re.I)
FULL_RE = re.compile(r'^(?:GMMS[-_ ]?)?(\d{2,3})?[-_ ]?(?:GMMS[-_ ]?)?(?:VIDEO|V\d)?', re.I)
```
Rules, in order:
1. `mimeType == application/vnd.google-apps.document` → `notes`
2. `mimeType == audio/mpeg` → `audio`
3. `CLIP_RE` matches → `clip`, `clip_index` from the capture group
4. filename contains "audiogram" → `audiogram`
5. any other `video/mp4` → `full_video`

Episode number comes from the **nearest ancestor folder whose title matches `EP_FROM_FOLDER`**, not from the filename — filenames lie (`GMMS-EP139-S3` sits in folder `139`, but `GMMS_121_V1` sits in folder `121` with a different pattern).

Log any file the classifier cannot place and continue. Never crash the indexer on an unexpected name.

### 2.3 Postability gate
```
postable = kind in ('clip','audiogram')
           and size_bytes <= 900_000_000        # IG API practical ceiling
           and 3 <= duration_sec <= 90          # IG Reels sweet spot
           and aspect in ('9:16','1:1')
```
`full_video` is **never postable** — 1.5 GB episodes are not Instagram posts. They are the source for Wave 5 auto-clipping.

`duration_sec`, `width`, `height` are unknown until the file is probed. Probe with `ffprobe` on first download and write back. Until probed, `postable` is null and the asset is not selectable — **fail closed, never post an unprobed file.**

### 2.4 The indexer job
`agent/jobs/index_podcast_library.py` — runs nightly in the existing daily draw, and on demand.
Walks the root, upserts every asset, marks assets whose Drive id has disappeared as `postable=false, reject_reason='removed_from_drive'`. Idempotent. Logs a one-line summary to `#ops`: episodes seen, clips found, newly postable, rejected with reasons.

---

## WAVE 3 — Selection (`agent/podcast_selector.py`)

```python
def pick_clip(exclude_recent_days: int = 120) -> PodcastAsset | None:
    """Least-used, longest-unused postable clip. Never the same clip inside 120 days."""
```
Order by `used_count ASC, last_used_at ASC NULLS FIRST`. Skip anything used inside `exclude_recent_days`.

Rails:
- **Reuse cooldown 120 days**, and never the same *episode* twice inside 21 days — four clips from one episode dumped in a week reads as a loop, which is the exact failure we are fixing.
- Stamp `used_count += 1` and `last_used_at` only when the row is **staged**, and roll it back if the coach denies the post. A denied post must return to the pool.
- If the pool is empty, fire ONE deduped alert (`podcast clip pool empty`) and let the slot fall through to the existing category logic. **Never repost a clip to fill a gap.**

---

## WAVE 4 — Caption grounding (`agent/podcast_caption.py`)

The caption must come from the actual episode, not invention. That is the whole point.

1. Pull the episode's notes Doc via `export_doc_text(notes_doc_id)`.
2. Extract: episode number, title, guest name if present, and 3–5 concrete claims.
3. Draft to the existing B2B rules: hook first line, short lines, 150–500 chars, **exactly one ask**, zero banned dashes via `copy_gate`.
4. Category is `podcast`. Subject to the existing ≤25% cap — this build is not a licence to make the feed all podcast.
5. Guest episodes: tag the guest's handle **only if the handle is in the mentions allowlist.** No guessing handles.

**Hard rail:** if the notes Doc is missing or exports empty, the slot does not stage. One deduped alert. Echo does not write a caption about an episode it cannot read.

---

## WAVE 5 — Auto-clipping (deferred, do not build in Wave 1–4)

For the ~84 older episodes with no clips: transcribe the full video, find high-signal 30–60s segments, cut to 9:16 with captions burned in.

Do not start this until Waves 1–4 are live and the ~60 existing clips are flowing. Those clips are 15 weeks of runway. Build the simple thing first.

---

## Wiring into the existing planner

`real_month_planner` gains one source for `podcast` category slots:
```
podcast slot -> podcast_selector.pick_clip()
             -> none? fall through to existing logic + alert
             -> got one? podcast_caption.draft()
                       -> copy_gate.violations empty?
                       -> download to temp, ffprobe, validate
                       -> zernio media_generate_upload_link -> upload
                       -> media_check_upload_status until ready
                       -> validate_post
                       -> stage calendar row as PENDING
```
Recheck at publish, same as every other row: caption non-empty, media still ready, cooldown clean. A failing row flips back to `pending` with a reject reason and one deduped alert.

**Fix the captionless-Instagram defect before this ships.** A third of Echo's current IG output publishes with an empty caption. Pushing 60 video posts through a pipeline with that bug means 20 silent reels.

Temp files: download to `/tmp`, delete after upload succeeds or fails. A 250 MB clip per post at 4/week is fine; never accumulate.

---

## Flags and rollout
| Flag | Default | Effect |
|---|---|---|
| `PODCAST_LIBRARY_INDEX` | ON | indexer runs, writes `podcast_asset`, posts nothing |
| `PODCAST_LIBRARY_STAGE` | OFF | selector + caption + upload + stage as pending |

Ship with index ON and stage OFF for one week. Read the index summary, confirm clip counts and probe results look right, then flip stage ON.

---

## Tests
- `test_podcast_classifier.py` — every observed naming pattern in §0 classifies correctly, including `GMMS-EP139-S3.mp4`, `GMMS136-S1.mp4`, `GMMS-140-S1.mp4`, `140-audiogram.mp4`, `141-Video.mp4`, `GMMS_121_V1.mp4`, `86_GMMS.mp4`. Unknown names log and skip, never raise.
- `test_postability.py` — 1.59 GB full episode rejected; 12 MB audiogram accepted; unprobed asset not selectable.
- `test_selector_cooldown.py` — same clip cannot return inside 120 days; same episode cannot return inside 21 days; denied post returns to the pool.
- `test_caption_grounding.py` — missing notes Doc ⇒ no stage, one alert; drafted caption is non-empty, has exactly one ask, passes `copy_gate`.
- `test_pending.py` — every staged row lands `pending`. The tap is untouched.
- Regression: podcast category still cannot exceed 25% of a month.

## Definition of done
- [ ] Service account created, `Podcast Episodes` shared to it as Viewer, key in secrets
- [ ] Indexer green, ~60 clips marked postable with real probe data
- [ ] Selector honours both cooldowns
- [ ] Captions ground in the notes Doc, or the slot does not stage
- [ ] Captionless-IG defect fixed and regression-tested
- [ ] All staged rows land `pending`
- [ ] Podcast ≤25% of any month

## What Blake has to do
1. Approve creating the service account.
2. Share `Podcast Episodes` to the service-account email as **Viewer**.
3. Nothing else. No re-uploading, no renaming, no reorganising — the resolver handles the naming mess as-is.
