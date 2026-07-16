"""
Brand voice intake template generator (Stage 2 T3).

Emits a client-fillable intake document. StoryBrand-shaped. No em dashes, no
en dashes, no hyphens in copy sentences. No word vendor (use: tools, systems,
logins, companies). Every line of rendered output is dash-free in copy.

  python -m agent voice-template [--out <path>]
"""

import os

_DEFAULT_OUT = os.path.join("brand_voice", "BRAND_VOICE_INTAKE.md")

# Eight sections: name, prompt, example.
# Every prompt and example line is free of em dashes, en dashes, and hyphens
# in sentence copy. Colons and commas replace any dash construction.
TEMPLATE_SECTIONS = [
    {
        "name": "Gym Name and One-Line Promise",
        "prompt": (
            "Write your gym name and one sentence that states the single result "
            "your members get. Lead with the outcome, not the method. "
            "Example: '[Gym Name]: We turn complete beginners into confident "
            "athletes in 90 days."
        ),
        "example": "CrossTown Fitness: We help busy professionals lose 20 pounds and keep it off.",
    },
    {
        "name": "The Member You Serve",
        "prompt": (
            "Describe your best member like a person, not a demographic. Include: "
            "age range, daily life, what they struggle with, what result they want, "
            "and what almost stopped them from walking in the first time."
        ),
        "example": (
            "She is 38, works full time, has two kids, and has tried every diet "
            "program out there. She wants to feel strong again, not just skinny. "
            "She almost did not join because she was afraid of looking foolish "
            "in front of people who already knew what they were doing."
        ),
    },
    {
        "name": "The Problem You Solve",
        "prompt": (
            "State the external problem (the visible struggle), the internal problem "
            "(how it makes them feel), and the philosophical problem (why it should "
            "not be this hard). Use plain sentences, no lists required."
        ),
        "example": (
            "External: They cannot find a program that fits their schedule and "
            "actually sticks. Internal: They feel like they have already failed "
            "before they start. Philosophical: No one should have to figure out "
            "their health alone."
        ),
    },
    {
        "name": "Your Proof",
        "prompt": (
            "List the real results and credentials that make you credible. Numbers "
            "only when you can verify them. Include member wins you have permission "
            "to share, years in business, certifications, and any press or awards. "
            "Do not include any number you cannot document."
        ),
        "example": (
            "Open since 2018. Over 400 members coached. "
            "Average member loses 14 pounds in the first 60 days. "
            "Featured in the Carmel Gazette fitness guide 2025."
        ),
    },
    {
        "name": "Your Offers",
        "prompt": (
            "List each program or membership you want to promote. For each one: "
            "the name, who it is for, what it includes, and the call to action "
            "you want the post to drive. Do not include prices here unless they "
            "are already public on your website."
        ),
        "example": (
            "Foundations (6-week beginner program): for people with no gym "
            "experience who want a safe, coached start. Includes 3 sessions per "
            "week plus a nutrition orientation. Call to action: Book a free intro."
        ),
    },
    {
        "name": "Tone and Voice",
        "prompt": (
            "Describe how you talk to members in person. List words and phrases "
            "you actually say. List words you would never say. Note whether you "
            "use emoji, how formal or casual your posts should feel, and any "
            "brand you admire for its tone (describe the tone, not the brand name)."
        ),
        "example": (
            "We say: real, honest, community, earned, strong, show up. "
            "We never say: crush it, beast mode, shred, summer body, "
            "transformation (overused). "
            "Casual but never sloppy. No emoji in captions. "
            "Sound like the coach who remembers your name."
        ),
    },
    {
        "name": "What to Avoid",
        "prompt": (
            "List anything Echo must never say or show: expired promotions, "
            "competitors by name, pricing you keep private, member names without "
            "written permission, health claims that require a disclaimer, or any "
            "topic that is off limits for your audience."
        ),
        "example": (
            "Never name competitors. Never post the $99 founding member price "
            "(that offer closed in 2024). Never post a member photo without "
            "written release on file. No weight loss percentage claims."
        ),
    },
    {
        "name": "Call to Action",
        "prompt": (
            "Write one to three call-to-action lines in your own voice. Include "
            "the link or booking method you want people to use. If you have "
            "hashtags you already own or want to build, list them here."
        ),
        "example": (
            "Book your free intro session at [link]. "
            "Text START to [number] to get the schedule. "
            "Hashtags: #CrossTownFitness #CarmelGym #BeginnersWelcome"
        ),
    },
]

_FOOTER = """
---

**Fabrication gate reminder:** Every stat, price, and claim in your posts must
come from this document or a source you submit. Echo will never invent a number,
a result, or an offer. If a section is blank, the posts for that topic are held
until you fill it in.

Return this file to your LASSO account manager or drop it in the client intake folder.
"""


def render_template(out_path=None):
    """
    Write the fillable brand voice intake template to out_path.
    Default path: brand_voice/BRAND_VOICE_INTAKE.md.
    Returns the path written.

    Every line of output is free of em dashes (U+2014), en dashes (U+2013),
    and hyphens used as copy connectors. The word vendor never appears.
    """
    if out_path is None:
        out_path = _DEFAULT_OUT

    lines = [
        "# Brand Voice Intake",
        "",
        "Fill in each section in your own words. The more specific you are, the "
        "better Echo matches your voice. Blank sections become open TODOs in your "
        "draft bible: the posts for that topic are held until you complete them.",
        "",
        "All claims require proof. Do not include a number or a result you cannot "
        "document. The fabrication gate at the bottom of this file is the rule.",
        "",
    ]

    for i, section in enumerate(TEMPLATE_SECTIONS, start=1):
        lines.append(f"## {i}. {section['name']}")
        lines.append("")
        lines.append(section["prompt"])
        lines.append("")
        lines.append(f"_Example: {section['example']}_")
        lines.append("")
        lines.append("**Your answer:**")
        lines.append("")
        lines.append("")

    lines.append(_FOOTER.strip())
    lines.append("")

    content = "\n".join(lines)

    # Safety assertion: no em dash or en dash should survive the render.
    assert "—" not in content, "em dash found in rendered template"
    assert "–" not in content, "en dash found in rendered template"

    os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(content)

    return out_path
