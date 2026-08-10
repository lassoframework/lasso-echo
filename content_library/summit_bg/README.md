# Summit bold card backgrounds

Drop real event-scene photos here to composite behind the BOLD summit cards
(gym owners at a summit in athleisure). The code consumes this directory; it
never generates or invents images.

Layout:

- `feed/`  jpg/png used behind the 1080x1080 feed cards
- `story/` jpg/png used behind the 1080x1920 story cards

Selection is deterministic: each concept maps to one photo by a stable hash of
the concept id, so cards vary across concepts and a given concept always uses the
same photo. Filenames do not matter (the code sorts them for a stable order); the
count does.

If a subdir is empty or missing, cards fall back to the flat dark bold base and
never crash. A corrupt or unreadable image also falls back to flat dark.

Wired via `summit_render.SUMMIT_BG_DIR` and threaded through
`summit_rebuild.render_and_host_all(background_dir=...)`.
