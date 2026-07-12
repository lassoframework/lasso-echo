# Railway start commands for the Echo services (same repo, two services).
# worker: the always-on listener — Slack approvals (Socket Mode) + the daily
#         scheduler. Draft-only by default (AGENT_PUBLISH_ENABLED OFF).
#         This is the startCommand in railway.json (the default service).
# web:    the texted-link intake upload page. Its OWN Railway service; set its
#         start command to this line by hand (docs/INTAKE_DEPLOY.md is the
#         runbook). R2 only, never /data. Dark until AGENT_INTAKE_ENABLED=true.
#         Health check: GET /healthz answers 200 even while the flag is OFF.
worker: /opt/venv/bin/python -m agent listen
web: /opt/venv/bin/python -m agent intake-web
