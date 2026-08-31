#!/usr/bin/env python3
"""Send a Slack message AS ECHO (never as Blake).

Client-facing messages must come from the Echo app (bot user U0BE39F02KV, shows as
"Echo APP"), not from a human account. Posting through the Claude Slack connector
sends as whoever is driving it and stamps "Sent using @Claude" — that is for internal
notes only, never for a gym owner.

The token lives ONLY in the deployed worker env (AGENT_SLACK_BOT_TOKEN on the `echo`
Railway service). This script pulls it into memory and never prints or stores it.

Usage:
    python3 scripts/echo_slack.py <channel_id> <message_file>
    python3 scripts/echo_slack.py --whoami

Find a channel id with the Slack search tools (a group DM id looks like C0BTPBQD6SJ).
Write the message to a file first so the text is reviewable before it goes out.
"""
import json
import subprocess
import sys
import urllib.request

REPO = "/Users/blakeruff/lasso-echo-work"


def _token():
    out = subprocess.run(
        ["railway", "variables", "--service", "echo", "--json"],
        capture_output=True, text=True, cwd=REPO,
    )
    if out.returncode != 0:
        raise SystemExit("railway variables failed; run from a linked machine")
    tok = json.loads(out.stdout).get("AGENT_SLACK_BOT_TOKEN", "")
    if not tok:
        raise SystemExit("AGENT_SLACK_BOT_TOKEN not set on the echo service")
    return tok


def _api(tok, method, payload=None):
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="POST",
    )
    return json.load(urllib.request.urlopen(req))


def main(argv):
    tok = _token()
    if argv and argv[0] == "--whoami":
        who = _api(tok, "auth.test")
        print("sending as:", who.get("user"), who.get("user_id"))
        return 0
    if len(argv) != 2:
        print(__doc__)
        return 2
    channel, path = argv
    text = open(path, encoding="utf-8").read().strip()
    if not text:
        raise SystemExit("refusing to send an empty message")
    who = _api(tok, "auth.test")
    resp = _api(tok, "chat.postMessage", {"channel": channel, "text": text})
    print(f"sent as {who.get('user_id')} | ok: {resp.get('ok')} "
          f"| error: {resp.get('error')} | ts: {resp.get('ts')}")
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
