"""
One-shot: post approval cards for specific summit posts to Slack.
Run via: railway run .venv/bin/python force_card.py [week_numbers...]
Example: railway run .venv/bin/python force_card.py 1
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from agent.summit_queue import SUMMIT_POSTS, ACCOUNTS, _draft_id
from agent.drafter import Draft, DraftStatus
from agent.accounts import get_account
from agent import schedule as sched
from agent.slack_surface import SlackPoster

weeks = [int(w) for w in sys.argv[1:]] if sys.argv[1:] else [1]

manifest_path = os.path.join(ROOT, "summit_manifest.json")
with open(manifest_path) as f:
    manifest = json.load(f)

poster = SlackPoster()
sent = 0

for post in SUMMIT_POSTS:
    if post["week"] not in weeks:
        continue
    url = manifest[post["filename"]]
    day = post["date"]
    scheduled_for = sched.scheduled_for(day)
    for acct in ACCOUNTS:
        acct_obj = get_account(acct)
        did = _draft_id(acct, post["filename"], day)
        d = Draft(
            draft_id=did,
            account_key=acct,
            platform=acct_obj.platform if acct_obj else acct,
            caption=post["caption"],
            hashtags=[],
            creative_path=post["filename"],
            creative_public_url=url,
            scheduled_for=scheduled_for,
            status=DraftStatus.PENDING,
            day_key=day,
            draft_type="feed",
        )
        poster.post_approval_card(d)
        print(f"  card sent: {did}  {acct}  week {post['week']}")
        sent += 1

print(f"\n{sent} card(s) posted to Slack.")
