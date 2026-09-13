#!/usr/bin/env bash
# Renders the 30 LASSO cards (book, lasso, summit, echo, website, nurture)
# through the REAL Astra pipeline. Called by .github/workflows/adhoc_render_cards.yml
# (mode: batch30), which supplies OPENAI_API_KEY and AGENT_ASTRA_STYLE_FREEDOM=true
# as job env. Nothing publishes, nothing is queued -- render-card's own guards apply.
# All headlines/facts/CTAs are the exact approved lines from
# knowledge/full_gym_book.md, brand_voice/lasso_now.md, and
# agent/summit_rebuild.py.SUMMIT_CONCEPTS. No fabrication.
set -e
mkdir -p astra_renders

echo "[ 1/30] book: Paid ads are not magic. They are math."
python -m agent render-card --headline 'Paid ads are not magic. They are math.' --cta 'Save this for later.' --account lasso --out astra_renders/book --fact 'Verbatim pull quote from our book The Full Gym' --fact 'The Three Levers of Growth: churn, sales, leads' --fact 'Paid ads are the last lever, not the first'

echo "[ 2/30] book: Fix churn. Fix sales. Then add leads."
python -m agent render-card --headline 'Fix churn. Fix sales. Then add leads.' --cta 'Save this for later.' --account lasso --out astra_renders/book --fact 'The Three Levers of Growth from our book The Full Gym' --fact 'Diagnose in order: retention, then conversion, then lead flow' --fact 'Healthy churn is 3 to 6 percent. Organic close rate is 70 to 80 percent'

echo "[ 3/30] book: If you are speaking to everyone, you are speaking to no one."
python -m agent render-card --headline 'If you are speaking to everyone, you are speaking to no one.' --cta 'Send this to a gym owner who needs it.' --account lasso --out astra_renders/book --fact 'The client avatar law from our book The Full Gym' --fact 'Your avatar is not your client'"'"'s data, it is their desires' --fact 'If your audience says that is me, you have already sold them'

echo "[ 4/30] book: Gaining five and losing five means you are stuck."
python -m agent render-card --headline 'Gaining five and losing five means you are stuck.' --cta 'Save this for later.' --account lasso --out astra_renders/book --fact 'Verbatim pull quote from our book The Full Gym' --fact 'Churn is the first lever, before sales and before leads' --fact 'Industry average is 4 to 5 new members per month'

echo "[ 5/30] book: The ad earned the click. Your follow up earns the member."
python -m agent render-card --headline 'The ad earned the click. Your follow up earns the member.' --cta 'Save this for later.' --account lasso --out astra_renders/book --fact 'Verbatim pull quote from our book The Full Gym' --fact 'Leads are not revenue. They are the beginning of a process' --fact 'First touch within five minutes'

echo "[ 6/30] lasso: The 5 tools running your gym should be one."
python -m agent render-card --headline 'The 5 tools running your gym should be one.' --cta 'Take the 2 minute quiz.' --account lasso --out astra_renders/lasso --fact 'Ads, lead nurture, your website, your social, and your reporting' --fact 'LASSO puts it all in one place, done for you' --fact 'Stop duct taping tools together'

echo "[ 7/30] lasso: We get the leads. We nurture them. All you do is sell."
python -m agent render-card --headline 'We get the leads. We nurture them. All you do is sell.' --cta 'Book a free call and we will look at your numbers.' --account lasso --out astra_renders/lasso --fact 'The job is closing members, not building funnels' --fact 'Done for you ads, nurture, website, social and reporting' --fact 'You do the one thing only you can do'

echo "[ 8/30] lasso: You did not open a gym to run ads at 11pm."
python -m agent render-card --headline 'You did not open a gym to run ads at 11pm.' --cta 'Take the 2 minute quiz.' --account lasso --out astra_renders/lasso --fact 'Most gym owners are buried in work they never signed up for' --fact 'Running ads, chasing leads, fixing the website, posting to social' --fact 'That is not the job. The job is closing members'

echo "[ 9/30] lasso: Every lead, every post, every result. One screen."
python -m agent render-card --headline 'Every lead, every post, every result. One screen.' --cta 'Take the 2 minute quiz.' --account lasso --out astra_renders/lasso --fact 'Your leads, your content, and your reporting live in one place' --fact 'One login runs your whole growth engine' --fact 'The LASSO portal'

echo "[10/30] lasso: Built by gym owners, for gym owners."
python -m agent render-card --headline 'Built by gym owners, for gym owners.' --cta 'Book a free call and we will look at your numbers.' --account lasso --out astra_renders/lasso --fact 'We run the same system on ourselves before we ever hand it to you' --fact 'No guesswork and no bait and switch' --fact 'Trusted by 1,000+ gym owners'

echo "[11/30] summit: I saved you a seat."
python -m agent render-card --headline 'I saved you a seat.' --cta lassoframework.com/summit --account lasso --out astra_renders/summit --fact '100 seats. 10 leaders. 2 days. You leave with a plan' --fact 'November 7 and 8. Virgin Hotel Nashville' --fact 'When the room is full there is no waitlist'

echo "[12/30] summit: A plan, not a notebook."
python -m agent render-card --headline 'A plan, not a notebook.' --cta lassoframework.com/summit --account lasso --out astra_renders/summit --fact 'By Sunday you walk out with your 2027 growth plan' --fact 'Your revenue target and the member math' --fact 'Your one broken funnel leg and the fix'

echo "[13/30] summit: 100 owners. 10 leaders. 2 days. 1 plan."
python -m agent render-card --headline '100 owners. 10 leaders. 2 days. 1 plan.' --cta lassoframework.com/summit --account lasso --out astra_renders/summit --fact 'November 7 and 8. Virgin Hotel Nashville' --fact 'You did not come to Nashville for notes. You came for a plan' --fact 'Ten sessions, ten leaders, one plan you build page by page'

echo "[14/30] summit: More ad spend will not fix a broken funnel."
python -m agent render-card --headline 'More ad spend will not fix a broken funnel.' --cta lassoframework.com/summit --account lasso --out astra_renders/summit --fact 'Leads to book at 40 percent or better' --fact 'Book to show at 50 percent. Show to close at 70 percent' --fact 'Fix the weakest leg, then scale'

echo "[15/30] summit: A goal without math is a wish."
python -m agent render-card --headline 'A goal without math is a wish.' --cta lassoframework.com/summit --account lasso --out astra_renders/summit --fact 'Set the target, subtract today, divide by revenue per member' --fact 'Then apply your close rate for leads per month' --fact 'Now you have a number, not a hope'

echo "[16/30] echo: We run your social media for you."
python -m agent render-card --headline 'We run your social media for you.' --cta 'Take the 2 minute quiz.' --account lasso --out astra_renders/echo --fact 'We plan the month, draft every post, and track what is working' --fact 'A human approves every post before it goes live' --fact 'You get the results without doing the work'

echo "[17/30] echo: A human approves every post before it goes live."
python -m agent render-card --headline 'A human approves every post before it goes live.' --cta 'Take the 2 minute quiz.' --account lasso --out astra_renders/echo --fact 'Nothing publishes without a person saying yes' --fact 'We plan the month and draft every post' --fact 'Done for you organic social'

echo "[18/30] echo: We plan the month, draft every post, and track what is worki"
python -m agent render-card --headline 'We plan the month, draft every post, and track what is working.' --cta 'Save this for later.' --account lasso --out astra_renders/echo --fact 'Done for you organic social from LASSO' --fact 'A human approves every post before it goes live' --fact 'You get the results without doing the work'

echo "[19/30] echo: You get the results without doing the work."
python -m agent render-card --headline 'You get the results without doing the work.' --cta 'Book a free call and we will look at your numbers.' --account lasso --out astra_renders/echo --fact 'We do the heavy lifting on your social' --fact 'We plan the month, draft every post, and track what is working' --fact 'A human approves every post before it goes live'

echo "[20/30] echo: Posting to social is not the job."
python -m agent render-card --headline 'Posting to social is not the job.' --cta 'Take the 2 minute quiz.' --account lasso --out astra_renders/echo --fact 'Most gym owners are buried in work they never signed up for' --fact 'The job is closing members' --fact 'We run your social media for you'

echo "[21/30] website: Facebook leads are not junk. Unmanaged leads are."
python -m agent render-card --headline 'Facebook leads are not junk. Unmanaged leads are.' --cta 'Send this to a gym owner who needs it.' --account lasso --out astra_renders/website --fact 'From our book The Full Gym, on forms and websites' --fact 'Sending traffic to a weak website kills conversion' --fact 'The most expensive lead is the one you get and then lose'

echo "[22/30] website: The most expensive lead is the one you get and then lose."
python -m agent render-card --headline 'The most expensive lead is the one you get and then lose.' --cta 'Save this for later.' --account lasso --out astra_renders/website --fact 'From our book The Full Gym, on forms and websites' --fact 'A weak website does not improve lead quality, it kills conversion' --fact 'Your website is one of the five tools LASSO runs for you'

echo "[23/30] website: A weak website does not lower lead quality. It kills convers"
python -m agent render-card --headline 'A weak website does not lower lead quality. It kills conversion.' --cta 'Take the 2 minute quiz.' --account lasso --out astra_renders/website --fact 'From our book The Full Gym, on forms and websites' --fact 'Turn off autofill, turn on SMS verification' --fact 'A more expensive lead that shows up is cheaper than a cheap lead that ghosts you'

echo "[24/30] website: Confused people do not convert."
python -m agent render-card --headline 'Confused people do not convert.' --cta 'Send this to a gym owner who needs it.' --account lasso --out astra_renders/website --fact 'From our book The Full Gym' --fact 'Clarity beats creativity. If you confuse, you lose' --fact 'Your website, done for you, as one of the five tools'

echo "[25/30] website: Clarity beats creativity."
python -m agent render-card --headline 'Clarity beats creativity.' --cta 'Save this for later.' --account lasso --out astra_renders/website --fact 'From our book The Full Gym' --fact 'People do not buy because your gym is cool' --fact 'They buy because you solve a problem they cannot solve alone'

echo "[26/30] nurture: 71.9% booked vs an 18.5% industry average."
python -m agent render-card --headline '71.9% booked vs an 18.5% industry average.' --cta 'Book a free call and we will look at your numbers.' --account lasso --out astra_renders/nurture --fact '297 leads nurtured across 4 gyms' --fact 'Same leads. Very different outcomes' --fact 'LASSO lead nurture pilot stats'

echo "[27/30] nurture: First touch within five minutes."
python -m agent render-card --headline 'First touch within five minutes.' --cta 'Save this for later.' --account lasso --out astra_renders/nurture --fact 'The window of peak interest closes fast' --fact 'Move engaged leads from text to a phone call' --fact 'From our book The Full Gym'

echo "[28/30] nurture: It takes 7 to 10 exposures before someone decides."
python -m agent render-card --headline 'It takes 7 to 10 exposures before someone decides.' --cta 'Send this to a gym owner who needs it.' --account lasso --out astra_renders/nurture --fact 'The follow up law from our book The Full Gym' --fact 'A no today does not mean no forever' --fact 'Following up is not an interruption to the business'

echo "[29/30] nurture: Leads are not revenue. They are the beginning of a process."
python -m agent render-card --headline 'Leads are not revenue. They are the beginning of a process.' --cta 'Save this for later.' --account lasso --out astra_renders/nurture --fact 'Verbatim pull quote from our book The Full Gym' --fact 'Growth breaks at every step you have not clearly defined' --fact 'We get the leads. We nurture them. All you do is sell'

echo "[30/30] nurture: A no today does not mean no forever."
python -m agent render-card --headline 'A no today does not mean no forever.' --cta 'Send this to a gym owner who needs it.' --account lasso --out astra_renders/nurture --fact 'From our book The Full Gym, on sales psychology' --fact 'It takes 7 to 10 exposures before someone decides to do business with you' --fact 'Objections are not rejection, they are signals'

