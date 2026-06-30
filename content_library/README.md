# Content library (Stage 1, local)

Drop the client's uploaded creative here. One media file per post.

- Images: .jpg .jpeg .png .gif .webp
- Video:  .mp4 .mov .m4v

Optional sidecar with the SAME name carries client-provided facts (never invented):

`founders_class.jpg` + `founders_class.json`:
```json
{ "note": "Founders class kicks off Saturday 9am. First 20 members only.",
  "public_url": "https://your-cdn.com/founders_class.jpg" }
```
Or a plain `founders_class.txt` holding just the note.

`public_url` is REQUIRED for Instagram publishing (Meta fetches media by URL).
Local files must be hosted first. See AGENT_README.md.
