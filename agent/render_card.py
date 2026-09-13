"""
render_card.py — render ONE Astra card on demand, through Echo's real engine.

Blake, 2026-09-13: he reviewed 30 style-freedom samples that were rendered by
calling the image model DIRECTLY (no OPENAI_API_KEY in that sandbox, so the
gpt-6-astra brief-reading layer never ran). This is the honest version: it
builds the SAME brief the scheduled draw builds and hands it to
image_engine.generate_image, so gpt-6-astra actually reads it and art directs
before Sunburst draws a pixel.

WHAT IT DOES NOT DO, and must never do:
  - it does not publish, schedule, or enqueue anything
  - it does not touch the approval queue or any calendar row
  - it invents nothing: a headline and at least one approved fact are REQUIRED,
    matching the block-on-missing-input contract the drafter uses

It writes a PNG plus a .txt sidecar carrying the exact brief, the style
selection, the engine and model that served it, and Astra's revised prompt when
the provider returns one. The sidecar is the audit trail for a hand render.
"""
import os
import re
import sys
import time

from . import config


def _slug(text, limit=48):
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return (s[:limit] or "card")


def _parse(argv):
    """Hand-rolled to match the rest of __main__'s CLI style (no argparse)."""
    opts = {
        "headline": "", "facts": [], "cta": "", "surface": "feed post",
        "canvas": None, "composition": None, "accent": None, "style_key": None,
        "out": "astra_renders", "count": 1, "locked": False, "brief_only": False,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        def nxt():
            if i + 1 >= len(argv):
                raise SystemExit(f"render-card: {a} needs a value")
            return argv[i + 1]
        if a in ("--headline", "-H"):
            opts["headline"] = nxt(); i += 2; continue
        if a in ("--fact", "-f"):
            opts["facts"].append(nxt()); i += 2; continue
        if a == "--cta":
            opts["cta"] = nxt(); i += 2; continue
        if a == "--surface":
            opts["surface"] = nxt(); i += 2; continue
        if a == "--story":
            opts["surface"] = "instagram story"; i += 1; continue
        if a == "--canvas":
            opts["canvas"] = nxt(); i += 2; continue
        if a == "--composition":
            opts["composition"] = nxt(); i += 2; continue
        if a == "--accent":
            opts["accent"] = nxt(); i += 2; continue
        if a == "--style-key":
            opts["style_key"] = nxt(); i += 2; continue
        if a == "--out":
            opts["out"] = nxt(); i += 2; continue
        if a == "--count":
            opts["count"] = max(1, int(nxt())); i += 2; continue
        if a == "--locked":
            opts["locked"] = True; i += 1; continue
        if a == "--brief-only":
            opts["brief_only"] = True; i += 1; continue
        raise SystemExit(f"render-card: unknown argument {a!r}. Try --help.")
    return opts


USAGE = """\
render one Astra card through Echo's real engine (Astra -> retry -> Gemini).
Nothing publishes. Nothing is queued. Nothing is invented.

  python -m agent render-card --headline "..." --fact "..." [--fact "..."] [options]

required
  --headline TEXT      the one line rendered on the card
  --fact TEXT          an APPROVED supporting line; repeat for more. At least one.

options
  --cta TEXT           the call to action rendered in the button block
  --surface TEXT       default "feed post";  --story  is shorthand for 9:16
  --canvas NAME        pin the canvas     (cream navy split sky ink red duotone)
  --composition NAME   pin the composition(flat_editorial type_poster data_story
                                           diagram split_screen stack device)
  --accent NAME        pin the accent     (one_word one_node rule_line arrow_tip
                                           cta_button corner_block)
  --style-key TEXT     the key the deterministic picker hashes (default: headline)
  --count N            render N times (same card, to compare Astra's takes)
  --locked             render with style freedom OFF, for an A/B against the old look
  --brief-only         print the brief and exit. Renders nothing, costs nothing.
  --out DIR            output directory (default ./astra_renders)
"""


def run(argv):
    if any(a in ("--help", "-h", "help") for a in argv):
        print(USAGE)
        return 0
    opts = _parse(argv)

    # Block on missing input, the same contract the drafter uses. No fabrication.
    if not str(opts["headline"]).strip():
        print("render-card: BLOCKED, --headline is required. Nothing was rendered.")
        return 2
    if not [f for f in opts["facts"] if str(f).strip()]:
        print("render-card: BLOCKED, at least one --fact is required so the card "
              "has approved material to draw from. Nothing was rendered.")
        return 2

    from . import astra_prompt, grade_gate, image_engine

    freedom = not opts["locked"]
    style = None
    if freedom:
        try:
            style = astra_prompt.style_for(
                str(opts["style_key"] or opts["headline"]),
                canvas=opts["canvas"], composition=opts["composition"],
                accent=opts["accent"])
        except ValueError as exc:
            print(f"render-card: {exc}")
            return 2

    try:
        brief = astra_prompt.build_infographic_brief(
            opts["headline"], opts["facts"], cta=opts["cta"],
            surface=opts["surface"], freedom=freedom,
            style_key=opts["style_key"], canvas=opts["canvas"],
            composition=opts["composition"], accent=opts["accent"])
    except ValueError as exc:
        print(f"render-card: BLOCKED by a hard rule: {exc}")
        return 2

    grade = grade_gate.grade_card(brief, headline=opts["headline"])
    label = ("locked" if opts["locked"]
             else f"{style['canvas']}/{style['composition']}/{style['accent']}")
    print(f"style      : {label}")
    print(f"freedom    : {'ON' if freedom else 'OFF'} "
          f"(env AGENT_ASTRA_STYLE_FREEDOM={os.environ.get('AGENT_ASTRA_STYLE_FREEDOM', 'unset')})")
    print(f"grade gate : {'PASS' if grade.passed else 'FAIL ' + str(grade.failed_questions)}")
    print(f"engine     : {image_engine.engine_name()} "
          f"brief={image_engine.astra_brief_model()} "
          f"image={image_engine.astra_image_model()}")
    print(f"brief      : {len(brief)} chars")

    if opts["brief_only"]:
        print("\n" + "=" * 72 + "\n" + brief)
        return 0

    os.makedirs(opts["out"], exist_ok=True)
    base = _slug(opts["headline"])
    rc = 0
    for n in range(1, opts["count"] + 1):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        suffix = f"_{n}" if opts["count"] > 1 else ""
        stem = os.path.join(opts["out"], f"{base}_{stamp}{suffix}")

        result = image_engine.generate_image(
            brief,
            opts={"kind": "infographic", "surface": opts["surface"],
                  "has_text_overlay": True},
            subject=opts["headline"])

        if result is None:
            print(f"[{n}/{opts['count']}] NO IMAGE. The chain exhausted every engine "
                  "and marked this needs_human (ops alert + audit row written).")
            rc = 1
            continue

        png = stem + ".png"
        if result.image_bytes:
            with open(png, "wb") as fh:
                fh.write(result.image_bytes)
            where = png
        else:
            where = result.image_url or "(no bytes and no url)"

        with open(stem + ".txt", "w", encoding="utf-8") as fh:
            fh.write(f"style       : {label}\n")
            fh.write(f"freedom     : {'ON' if freedom else 'OFF'}\n")
            fh.write(f"engine      : {result.engine}\nmodel       : {result.model}\n")
            fh.write(f"latency_ms  : {result.latency_ms}\n")
            fh.write(f"cost_est    : {result.cost_estimate}\n")
            fh.write(f"grade       : {'PASS' if grade.passed else grade.failed_questions}\n")
            if result.revised_prompt:
                fh.write("\n--- ASTRA REVISED PROMPT (the brief-reading layer's own "
                         "art direction) ---\n" + result.revised_prompt + "\n")
            fh.write("\n--- BRIEF AS SENT ---\n" + brief + "\n")

        print(f"[{n}/{opts['count']}] {result.engine}:{result.model} "
              f"{result.latency_ms}ms  ->  {where}")
        if result.revised_prompt:
            print(f"            astra revised the prompt "
                  f"({len(result.revised_prompt)} chars, see the .txt sidecar)")
    return rc
