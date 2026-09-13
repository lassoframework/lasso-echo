#!/usr/bin/env bash
# One-off diagnostic: renders 5 real cards (one per topic pillar) through the
# real Astra pipeline (gpt-6-astra + gpt-image-2.5-sunburst). Every headline
# and fact below is pulled verbatim from cards.py's approved source list
# (knowledge/full_gym_book.md, brand_voice/lasso_now.md, website-kb.md stats).
# Nothing here is invented. See adhoc_render_cards.yml for how this is wired.
set -e

mkdir -p astra_renders

python -m agent render-card \
  --headline "Paid ads are not magic. They are math." \
  --fact "Verbatim pull quote from our book The Full Gym" \
  --fact "The Three Levers of Growth: churn, sales, leads" \
  --fact "Paid ads are the last lever, not the first" \
  --cta "Save this for later." \
  --account lasso \
  --out astra_renders/book

python -m agent render-card \
  --headline "We get the leads. We nurture them. All you do is sell." \
  --fact "The job is closing members, not building funnels" \
  --fact "Done for you ads, nurture, website, social and reporting" \
  --fact "You do the one thing only you can do" \
  --cta "Book a free call and we will look at your numbers." \
  --account lasso \
  --out astra_renders/lasso

python -m agent render-card \
  --headline "I saved you a seat." \
  --fact "100 seats. 10 leaders. 2 days. You leave with a plan" \
  --fact "November 7 and 8. Virgin Hotel Nashville" \
  --fact "When the room is full there is no waitlist" \
  --cta "lassoframework.com/summit" \
  --account lasso \
  --out astra_renders/summit

python -m agent render-card \
  --headline "We run your social media for you." \
  --fact "We plan the month, draft every post, and track what is working" \
  --fact "A human approves every post before it goes live" \
  --fact "You get the results without doing the work" \
  --cta "Take the 2 minute quiz." \
  --account lasso \
  --out astra_renders/echo

python -m agent render-card \
  --headline "71.9% booked vs an 18.5% industry average." \
  --fact "297 leads nurtured across 4 gyms" \
  --fact "Same leads. Very different outcomes" \
  --fact "LASSO lead nurture pilot stats" \
  --cta "Book a free call and we will look at your numbers." \
  --account lasso \
  --out astra_renders/nurture
