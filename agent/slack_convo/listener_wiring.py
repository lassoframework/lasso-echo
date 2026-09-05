"""
listener_wiring.py — attach the adapter to a Bolt App for one bot identity.

This is the only file in the package that knows slack_bolt exists. It:
  - registers `message` and `app_mention` listeners that hand each inbound event to
    adapter.handle_event() on a bounded worker pool (so the Socket Mode ack is never held
    up by a database or model call);
  - registers the `slack_convo_release` button action (a human tap on a hold notice flips
    the held reply row to ready);
  - runs the outbox loop (outbox.run_once) on its own thread with a SlackPoster-backed
    post();
  - counts every event TYPE it receives and prints a health line on a timer, so a Slack
    event subscription that is not enabled shows up as a zero instead of as silence. (This
    project's most repeated lesson: a safety net that ships inert looks exactly like one
    that is working. Make the absence visible.)

Registering more identities is a loop over identities.startable(): one Bolt App per bot
whose tokens are present. Echo's App is the one listener.py already builds; the others are
built here when their tokens appear in the environment.

DEDUPE KEY (decision D13): the row's slack_event_id is "<channel>:<ts>", not Slack's raw
event_id. Slack emits DISTINCT event_ids for the same human message when it is delivered as
both a `message` and an `app_mention` event (a mention in a channel does both), so the raw
id would let one message become two rows. channel:ts is the identity of a message in Slack;
it also catches redelivery and replay. The raw event_id is kept in the row's attachments.
"""
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .. import config
from . import adapter as _adapter
from . import answer_lane as _answer
from . import bus as _bus
from . import classifier as _cls
from . import identity_gate as _ig
from . import identities as _ids
from . import outbox as _outbox
from . import outreach as _outreach

HEALTH_EVERY_SECONDS = 15 * 60
OUTBOX_EVERY_SECONDS = 5
MAX_CONCURRENT_EVENTS = 4


# ---- live dependency construction -------------------------------------------------------

def _slack_user_info_factory(bot_token):
    def _info(uid):
        from slack_sdk import WebClient
        res = WebClient(token=bot_token).users_info(user=uid)
        u = (res.data or {}).get("user") or {}
        prof = u.get("profile") or {}
        return {"id": u.get("id"), "is_bot": bool(u.get("is_bot")),
                "email": prof.get("email") or "", "real_name": u.get("real_name") or "",
                "name": u.get("name") or "",
                "is_restricted": bool(u.get("is_restricted")),
                "is_ultra_restricted": bool(u.get("is_ultra_restricted"))}
    return _info


_EMAIL_OK = re.compile(r"^[A-Za-z0-9._+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _portal_lookup_factory(bus):
    """email -> {role, gyms:[{gym_id, relationship, account_key}]} from the portal's own
    tables, via the same Supabase REST the bus uses. None when unknown.

    V-m10: the match is case-insensitive (Slack and Clerk disagree on case for the same
    person) and the value is validated as a plain address first, so no PostgREST wildcard
    (`*`, `%`) or operator can ride in on a profile email."""
    def _lookup(email):
        e = (email or "").strip()
        if not e or not _EMAIL_OK.match(e):
            return None
        users = bus._get("app_users", {"email": f"ilike.{e}", "select": "id,role,email",
                                       "limit": "2"})
        users = [u for u in users or [] if (u.get("email") or "").lower() == e.lower()]
        if not users:
            return None
        u = users[0]
        assigns = bus._get("gym_assignments", {"app_user_id": f"eq.{u['id']}",
                                               "select": "gym_id,relationship"})
        gyms = []
        for a in assigns or []:
            gid = a.get("gym_id")
            key = ""
            if gid:
                tok = bus._get("echo_intake_tokens", {"gym_id": f"eq.{gid}",
                                                      "select": "echo_account_key",
                                                      "limit": "1"})
                key = (tok[0].get("echo_account_key") if tok else "") or ""
            gyms.append({"gym_id": gid, "relationship": a.get("relationship"),
                         "account_key": key})
        return {"role": u.get("role"), "gyms": gyms}
    return _lookup


# ---- outreach-release factories ---------------------------------------------------------
# D48-follow-up (Frame 1 audit MAJOR, closing here): a tap on a KIND_OUTREACH_REQUEST hold
# card used to route to outbox.release_held, which refuses that kind outright (outbox.py's
# own accepted-kinds check) and no-ops SILENTLY -- Blake would believe he approved an
# outreach that never actually sent. _on_release below now keys the dispatch on the held
# row's own attachments.kind and, for an outreach request, calls
# outreach.release_approved_outreach instead, matching what outreach.py's docstring already
# promises happens. These two factories are copied verbatim from echo_ticket_wiring.py
# rather than imported from it: echo_ticket_wiring.py itself imports THIS module (for
# _slack_user_info_factory / _portal_lookup_factory), so importing it back here would be
# circular. Keep both copies in sync if either Slack call shape ever changes.
def _open_group_dm_factory(poster):
    def open_group_dm(user_ids):
        resp = poster._send("https://slack.com/api/conversations.open",
                            {"users": ",".join(user_ids)})
        if not (resp or {}).get("ok"):
            return {"ok": False}
        return {"ok": True, "channel_id": (resp.get("channel") or {}).get("id") or ""}
    return open_group_dm


def _post_first_message_factory(poster):
    def post_first_message(channel_id, text):
        res = poster._chat_post(text=text, blocks=None, channel=channel_id)
        if not (res or {}).get("ok"):
            return {"ok": False}
        return {"ok": True, "ts": res.get("ts") or ""}
    return post_first_message


def _stamp_ticket_factory(bus):
    def stamp_ticket(ticket_id, *, channel_id, thread_ts, slack_user_id, bot_identity,
                     identity_kind):
        return bus.set_ticket(ticket_id, slack_channel_id=channel_id,
                              slack_thread_ts=thread_ts, slack_user_id=slack_user_id,
                              bot_identity=bot_identity, identity_kind=identity_kind)
    return stamp_ticket


def live_deps(identity, *, bus=None, log=print):
    bus = bus or _bus.Bus()
    bot_token = identity.env(identity.bot_token_env)
    info = _slack_user_info_factory(bot_token)
    lookup = _portal_lookup_factory(bus)
    operators = tuple(x for x in [config.APPROVER_SLACK_ID] if x)

    def resolve(uid):
        return _ig.resolve(uid, slack_user_info=info, portal_lookup=lookup,
                           operator_ids=operators)

    def answer(ticket, who, messages, question=None, answer_identity=None):
        # D50: `answer_identity` is which product's knowledge and reply voice drafts this
        # answer. It defaults to the identity that owns the ticket and can only ever be a
        # DIFFERENT BOT'S KNOWLEDGE, never a different gym: `ticket` and `who` are passed
        # straight through untouched, and answer_lane's own fetch_state keys every live fact
        # off who.account_key. There is no argument here through which another gym's data
        # could enter.
        return _answer.answer(ticket, who, messages, question,
                              identity=answer_identity or identity,
                              speaks_as=identity.name)

    return _adapter.Deps(
        bus=bus, identity=identity, resolve_identity=resolve,
        identity_enabled=lambda: config.slack_convo_identity_enabled(identity.name),
        client_reply_armed=lambda: config.slack_convo_client_reply_armed(identity.name),
        staff_reply_armed=lambda: config.slack_convo_staff_reply_armed(identity.name),
        daily_cap=config.slack_convo_daily_ticket_cap,
        open_window_days=config.slack_convo_open_window_days,
        answer=answer, classify_llm=build_classify_llm(identity, log=log),
        describe_gym=_describe_gym_factory(bus),
        auto_answer_armed=lambda: config.slack_convo_auto_answer_armed(identity.name),
        cross_product_armed=lambda: config.slack_convo_cross_product_routing_enabled(
            identity.name),
        log=log)


class NotWiredError(RuntimeError):
    """A capability is configured ON but nothing is actually behind it. Raised at boot."""


def build_classify_llm(identity, *, log=print, factory=None):
    """The production classifier LLM for one identity, or None when the flag is off.

    THE FIX, and the assertion that keeps it fixed (2026-09-05):

    This function replaces the literal `classify_llm=None` that sat in live_deps() above.
    While it was there, classifier.classify() could never reach its LLM branch in production,
    so every message the regexes did not recognise fell to ESCALATE by construction -- the
    "the classifier did not decide" flood in #fixer. The capability was written, tested,
    documented in config.slack_convo_model()'s own docstring, and wired to nothing.

    That is the THIRD instance of one pattern in this system (see DECISIONS.md D56):
    delivery_status='posting' (a state the code wrote and the DB constraint rejected),
    listener_watch (a loop that shipped and was never started), and this. The shape is always
    the same: the capability exists, a flag says it is on, and nothing between the flag and
    the work is actually connected -- and it looks EXACTLY like working software from the
    outside, because the fallback path is a legitimate one.

    So this refuses to boot rather than degrade: if the flag says the model classifier is on
    and we cannot build one, that is a broken deployment, not a quieter mode. The `factory`
    seam exists so a test can assert both branches with no key and no network."""
    if not config.slack_convo_classifier_llm_enabled(identity.name):
        if config.slack_convo_identity_enabled(identity.name):
            log(f"[slack-convo/{identity.name}] classifier LLM is OFF "
                f"(SLACK_CONVO_{identity.name.upper()}_CLASSIFIER_LLM): classification is "
                f"deterministic rules only, and anything they do not recognise will escalate "
                f"to a person rather than be answered")
        return None
    try:
        llm = (factory or _cls.default_classify_llm)()
    except Exception as e:  # noqa: BLE001 - surfaced as NotWiredError below, never swallowed
        raise NotWiredError(
            f"SLACK_CONVO_{identity.name.upper()}_CLASSIFIER_LLM is on but no classifier LLM "
            f"could be built ({type(e).__name__}). Set ANTHROPIC_API_KEY or turn the flag "
            f"off; refusing to run with the flag on and nothing behind it.") from e
    if llm is None:
        raise NotWiredError(
            f"SLACK_CONVO_{identity.name.upper()}_CLASSIFIER_LLM is on but the classifier LLM "
            f"is None. Refusing to boot: with the flag on and nothing behind it, every "
            f"message the deterministic rules miss silently escalates and #fixer fills with "
            f"'the classifier did not decide'.")
    assert_classifier_shape(llm, identity)
    log(f"[slack-convo/{identity.name}] classifier LLM wired "
        f"(model {config.slack_convo_model()})")
    return llm


def assert_classifier_shape(llm, identity):
    """The classifier's llm contract is (text) -> label. Anything else refuses to boot.

    RTF-2 (2026-09-05): echo_ticket_wiring passed answer_lane.default_llm -- signature
    (system, user, model=None) -- as the CLASSIFIER's llm. Every call raised TypeError,
    classify() caught it exactly as designed (a model fault escalates rather than
    dispatches), and the result was indistinguishable from "the classifier had nothing to
    say". A mis-shaped callable is the same failure as no callable at all, only harder to
    see, so it is checked in the same place and just as loudly."""
    try:
        import inspect
        sig = inspect.signature(llm)
    except (TypeError, ValueError):
        return  # a builtin/C callable: nothing to check, and never what we build here
    required = [p for p in sig.parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if len(required) != 1:
        raise NotWiredError(
            f"[{identity.name}] the classifier LLM has the wrong shape: it needs exactly one "
            f"required argument (text), but takes {len(required)} ({list(sig.parameters)}). "
            f"This is how the answer lane's (system, user) callable got wired into the "
            f"classifier and silently escalated every message; refusing to boot.")


def _describe_gym_factory(bus):
    """(gym_id) -> a human gym name for a #fixer card, or '' (never raises, never blocks)."""
    def _describe(gym_id):
        gid = str(gym_id or "").strip()
        if not gid:
            return ""
        try:
            rows = bus._get("gyms", {"id": f"eq.{gid}", "select": "name,slug", "limit": "1"})
        except Exception:  # noqa: BLE001
            return ""
        if not rows:
            return ""
        return str(rows[0].get("name") or rows[0].get("slug") or "").strip()
    return _describe


def dedupe_key(event):
    return f"{event.get('channel') or ''}:{event.get('ts') or event.get('event_ts') or ''}"


# ---- registration ---------------------------------------------------------------------

class ConvoWiring:
    def __init__(self, app, identity, deps, *, post=None, open_group_dm=None,
                 post_first_message=None, log=print):
        self.app = app
        self.identity = identity
        self.deps = deps
        self.log = log
        self.counts = Counter()
        # RT-m4: a bounded pool, not a thread per event. A burst of events queues here
        # instead of spawning without limit; the Socket Mode ack is still immediate.
        self._pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_EVENTS,
                                        thread_name_prefix=f"slack-convo-{identity.name}")
        self._post = post or self._default_post()
        # Injectable, like `post` above, so a test can supply fakes without a real Slack
        # client; built lazily off the identity's own bot token (never Blake's, never a
        # different identity's) only if the outreach-release path is actually exercised.
        self._open_group_dm = open_group_dm
        self._post_first_message = post_first_message
        self.__poster = None
        self._stop = threading.Event()
        self._boot_checks()

    def _boot_checks(self):
        # V-m1: the fixer channel is where every escalation and hold card lands. Unset, the
        # hold lane is a black hole. Say so at boot, not at the first held row.
        # DV1 (2026-09-03): this used to check the global AGENT_FIXER_CHANNEL_ID regardless
        # of identity, so a non-Echo identity with its OWN fixer_channel_env set (D29) got a
        # false "will mark failed" warning even though outbox._channel_for's fallback made
        # delivery work fine. Checks self.identity's own resolution now, same as delivery.
        if not (self.identity.fixer_channel() or config.fixer_channel_id()):
            self.log(f"[slack-convo/{self.identity.name}] WARNING "
                     f"{self.identity.fixer_channel_env} (and the AGENT_FIXER_CHANNEL_ID "
                     "fallback) are both unset: escalations and hold cards will mark failed "
                     "until one is set")
        if not config.ops_fix_channel_id():
            self.log(f"[slack-convo/{self.identity.name}] WARNING no ops-fix channel: fixer "
                     "requests will mark failed until AGENT_OPS_FIX_CHANNEL_ID is set")

    def _default_post(self):
        from ..slack_surface import SlackPoster
        token = self.identity.env(self.identity.bot_token_env)
        poster = SlackPoster(token=token)

        def post(channel, text, thread_ts=None, blocks=None):
            res = poster._chat_post(text=text, blocks=blocks, channel=channel,
                                    thread_ts=thread_ts)
            if not (res or {}).get("ok"):
                raise RuntimeError(f"slack post failed: {(res or {}).get('error')}")
            return res.get("ts")
        return post

    def _poster(self):
        if self.__poster is None:
            from ..slack_surface import SlackPoster
            token = self.identity.env(self.identity.bot_token_env)
            self.__poster = SlackPoster(token=token)
        return self.__poster

    # -- inbound --
    def _process(self, event, raw_event_id):
        try:
            d = _adapter.handle_event(event, dedupe_key(event), self.deps)
            self.counts[f"decision:{d.action}:{d.reason}"] += 1
            if not d.ignored:
                self.log(f"[slack-convo/{self.identity.name}] {d.reason} ticket={d.ticket_id} "
                         f"created={d.created} class={d.classification or '-'} "
                         f"out={','.join(d.outbound_kinds)} raw_event={raw_event_id}")
        except Exception as e:  # noqa: BLE001 - loud, never silent, never crashes Bolt
            self.counts["decision:error"] += 1
            self.log(f"[slack-convo/{self.identity.name}] handle_event FAILED "
                     f"{type(e).__name__}: {e}")

    def _on_event(self, body, event, etype):
        ev = dict(event or {})
        ev["type"] = etype
        self.counts[f"event:{etype}:{ev.get('channel_type') or '-'}"] += 1
        if not self.deps.identity_enabled():
            return  # flags off = today: count it, touch nothing
        raw_id = (body or {}).get("event_id") or ""
        ev["_raw_event_id"] = raw_id
        self._pool.submit(self._process, ev, raw_id)

    def register(self):
        app, identity = self.app, self.identity

        @app.event("message")
        def _on_message(body, event):
            self._on_event(body, event, "message")

        @app.event("app_mention")
        def _on_mention(body, event):
            self._on_event(body, event, "app_mention")

        @app.action(_outbox.RELEASE_ACTION_ID)
        def _on_release(ack, body, action):
            ack()
            actor = (body.get("user") or {}).get("id", "")
            if not config.APPROVER_SLACK_ID or actor != config.APPROVER_SLACK_ID:
                self.counts["release:refused_non_operator"] += 1
                return
            if not self.deps.identity_enabled():
                self.counts["release:refused_flag_off"] += 1
                return
            mid = (action or {}).get("value") or ""
            # Dispatch on the held row's OWN kind, not a single hardcoded handler: an
            # outreach_request is a different animal (it needs open_group_dm, which
            # outbox.release_held refuses to touch by design -- see outbox.py's own
            # accepted-kinds check) and must route to outreach.release_approved_outreach
            # instead, or the tap silently no-ops with nothing ever sent (the Frame 1
            # audit MAJOR this closes).
            row = self.deps.bus.message(mid)
            kind = ((row or {}).get("attachments") or {}).get("kind") or ""
            if kind == _adapter.KIND_OUTREACH_REQUEST:
                ok = self._release_outreach(mid, row, actor)
                self.counts[f"release:{'ok' if ok else 'noop'}"] += 1
                return
            ok = _outbox.release_held(self.deps.bus, mid, approved_by=actor,
                                      identity=identity, log=self.log)
            self.counts[f"release:{'ok' if ok else 'noop'}"] += 1

        @app.action(_outbox.RESOLVE_ACTION_ID)
        def _on_resolve(ack, body, action):
            ack()
            actor = (body.get("user") or {}).get("id", "")
            if not config.APPROVER_SLACK_ID or actor != config.APPROVER_SLACK_ID:
                self.counts["resolve:refused_non_operator"] += 1
                return
            if not self.deps.identity_enabled():
                self.counts["resolve:refused_flag_off"] += 1
                return
            tid = (action or {}).get("value") or ""
            # Frame 1 audit MAJOR (closing here): escalation_blocks() (outbox.py) has
            # rendered a "Resolved, tell them" button on every escalation card since D48
            # (#41), and its own docstring already promised this is "operator-gated like
            # the release tap" -- but nothing ever registered an @app.action handler for
            # RESOLVE_ACTION_ID, so every tap silently failed at the Slack layer (no ack()
            # ever ran) with resolve_and_notify() never invoked: Blake would tap the
            # button, see it error out, and the client would never be told anything.
            ok = _outbox.resolve_and_notify(self.deps.bus, tid, approved_by=actor,
                                            identity=identity, log=self.log)
            self.counts[f"resolve:{'ok' if ok else 'noop'}"] += 1

        self.log(f"[slack-convo/{identity.name}] registered (enabled="
                 f"{self.deps.identity_enabled()})")
        return self

    def _release_outreach(self, message_id, row, actor):
        """Tap handler for a KIND_OUTREACH_REQUEST hold card. Reconstructs `ticket` and
        `who` from the held row and its parent ticket, then calls
        outreach.release_approved_outreach -- the function this whole path exists to
        reach, keyed on attachments.held_kind as outreach.py's own docstring promises.
        Refuses (never raises) for every validation failure; release_approved_outreach
        itself re-checks identity/ticket ownership and every base eligibility gate at
        tap time, so nothing here is a substitute for those checks."""
        bus = self.deps.bus
        att = (row or {}).get("attachments") or {}
        ticket = bus.ticket((row or {}).get("ticket_id")) if row else None
        uid = att.get("slack_user_id") or (ticket or {}).get("slack_user_id") or ""
        who = self.deps.resolve_identity(uid) if uid else None
        open_group_dm = self._open_group_dm or _open_group_dm_factory(self._poster())
        post_first_message = (self._post_first_message
                              or _post_first_message_factory(self._poster()))
        result = _outreach.release_approved_outreach(
            message_id, ticket, who, self.identity,
            get_held_message=bus.message, open_group_dm=open_group_dm,
            post_first_message=post_first_message, record_outbound=bus.record_outbound,
            stamp_ticket=_stamp_ticket_factory(bus), mark_message=bus.mark_message,
            claim_message=getattr(bus, "claim_message", None), log=self.log)
        # Finding 3 (audit 3): `opened` is True on post_failed, so a tap that delivered
        # NOTHING was counted release:ok and logged nothing at all.
        delivered = bool(getattr(result, "delivered", False))
        if not delivered:
            self.log(f"[slack-convo/{self.identity.name}] outreach release did not deliver "
                     f"row={message_id} reason={result.reason}")
        return delivered

    # -- loops --
    def start_loops(self):
        threading.Thread(target=self._outbox_loop, daemon=True,
                         name=f"slack-convo-outbox-{self.identity.name}").start()
        threading.Thread(target=self._health_loop, daemon=True,
                         name=f"slack-convo-health-{self.identity.name}").start()
        return self

    def _outbox_loop(self):
        while not self._stop.is_set():
            try:
                if self.deps.identity_enabled():
                    s = _outbox.run_once(self.deps.bus, self._post, identity=self.identity,
                                         log=self.log)
                    for k, v in s.items():
                        if v:
                            self.counts[f"outbox:{k}"] += v
            except Exception as e:  # noqa: BLE001
                self.log(f"[slack-convo/{self.identity.name}] outbox loop error "
                         f"{type(e).__name__}")
            self._stop.wait(OUTBOX_EVERY_SECONDS)

    def _health_loop(self):
        while not self._stop.is_set():
            self._stop.wait(HEALTH_EVERY_SECONDS)
            if self._stop.is_set():
                break
            self.log(self.health_line())

    def classifier_health(self):
        """Audit 4, finding 7: default_classify_llm counts consecutive model failures, and
        the comment claimed the count was surfaced on the health line. Nothing read it -- a
        dead-key counter nobody reads is D56's own pattern inside the fix for it. Read here,
        and rendered by health_line below."""
        state = getattr(getattr(self.deps, "classify_llm", None), "failure_state", None)
        if not isinstance(state, dict):
            return {}
        n = state.get("consecutive_failures") or 0
        return {"classifier_llm_consecutive_failures": n,
                "classifier_llm": "DEAD" if n >= 3 else ("degraded" if n else "ok")}

    def health_line(self):
        seen = {k.split(":", 1)[1]: v for k, v in self.counts.items() if k.startswith("event:")}
        # the zeros are the point: a subscription that is not enabled shows here as absent
        for want in ("message:im", "message:mpim", "message:channel", "message:group",
                     "app_mention:-"):
            seen.setdefault(want, 0)
        other = {k: v for k, v in self.counts.items() if not k.startswith("event:")}
        other.update(self.classifier_health())
        return (f"[slack-convo/{self.identity.name}] health enabled="
                f"{self.deps.identity_enabled()} events={dict(sorted(seen.items()))} "
                f"other={dict(sorted(other.items()))}")

    def stop(self):
        self._stop.set()


def attach(app, identity_name="echo", *, deps=None, post=None, log=print):
    """Attach the adapter to an EXISTING Bolt App (listener.py's Echo app). Returns the
    wiring (already registered and looping) or None when the master flag is off -- in
    which case nothing is registered and the app is exactly as it was."""
    if not config.slack_convo_enabled():
        log(f"[slack-convo/{identity_name}] SLACK_CONVO_ENABLED is off; not attached")
        return None
    identity = _ids.get(identity_name)
    w = ConvoWiring(app, identity, deps or live_deps(identity, log=log), post=post, log=log)
    return w.register().start_loops()


def start_additional_identities(*, exclude=("echo",), log=print):
    """Build and start a Bolt App per additional identity whose tokens are present. Each
    gets its own Socket Mode connection. Echo is excluded because listener.py owns its App."""
    started = []
    if not config.slack_convo_enabled():
        return started
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        log("[slack-convo] slack_bolt not installed; additional identities not started")
        return started
    for ident in _ids.startable():
        if ident.name in exclude:
            continue
        # V-M9: tokens present is not consent. The per-identity flag gates the socket itself,
        # so an identity with config shipped and its flag OFF opens no connection at all.
        if not config.slack_convo_identity_enabled(ident.name):
            log(f"[slack-convo/{ident.name}] tokens present but "
                f"SLACK_CONVO_{ident.name.upper()}_ENABLED is off; not started")
            continue
        app = App(token=ident.env(ident.bot_token_env))
        w = ConvoWiring(app, ident, live_deps(ident, log=log), log=log).register().start_loops()
        threading.Thread(target=SocketModeHandler(app, ident.env(ident.app_token_env)).start,
                         daemon=True, name=f"slack-convo-socket-{ident.name}").start()
        started.append(w)
        log(f"[slack-convo/{ident.name}] socket mode started")
    return started
