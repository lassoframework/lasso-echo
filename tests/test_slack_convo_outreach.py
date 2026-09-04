"""
tests/test_slack_convo_outreach.py — ticket-initiated outreach (Blake's ruling item 3).

Blake's TESTS list: "outreach refuses on unresolved identity, outreach refuses when
reporter is not the client."
"""
from agent.slack_convo import identities as ids
from agent.slack_convo import identity_gate as ig
from agent.slack_convo import outreach


def _client(uid="U_CLIENT", email="owner@gym.com"):
    return ig.Identity(ig.CLIENT, uid, email=email, display="Gym Owner",
                       account_key="crossfitlocal", gym_id="g-1", reason="portal client_owner")


def _staff(uid="U_STAFF"):
    return ig.Identity(ig.STAFF, uid, email="blake@lassoframework.com", reason="operator list")


def _unknown(uid="U_UNK"):
    return ig.Identity(ig.UNKNOWN, uid, reason="no portal user for this email")


def _ticket(**over):
    row = {
        "id": "t-1", "source": "portal_form", "reporter": "owner@gym.com",
        "slack_user_id": "U_CLIENT", "raw_text": "my page shows the wrong hours",
        # D42: a real producer must positively assert provenance. The base fixture
        # asserts it so the PRE-EXISTING tests below (unresolved identity, reporter
        # mismatch, etc.) keep testing exactly what they said they test; the dedicated
        # D42 tests further down override this back to missing/False.
        "reporter_verified": True,
    }
    row.update(over)
    return row


# ---- eligibility (pure gate) ------------------------------------------------------------

def test_eligible_for_a_matched_client_from_a_non_slack_source():
    ok, reason = outreach.eligible(_ticket(), _client())
    assert ok is True and reason == "eligible"


def test_refuses_when_source_is_a_slack_conversation():
    ok, reason = outreach.eligible(_ticket(source="slack_conversation"), _client())
    assert ok is False and reason == "not_a_non_slack_source"


def test_refuses_an_unrecognised_source_rather_than_guessing_it_is_non_slack():
    ok, reason = outreach.eligible(_ticket(source="some_new_intake_nobody_reasoned_about"),
                                   _client())
    assert ok is False and reason == "not_a_non_slack_source"


def test_refuses_unresolved_identity_unknown():
    ok, reason = outreach.eligible(_ticket(), _unknown())
    assert ok is False and reason == "identity_unresolved"


def test_refuses_bot_identity():
    ok, reason = outreach.eligible(_ticket(), ig.Identity(ig.BOT, "U_BOT"))
    assert ok is False and reason == "identity_unresolved"


def test_refuses_when_who_is_none():
    ok, reason = outreach.eligible(_ticket(), None)
    assert ok is False and reason == "identity_unresolved"


def test_refuses_ambiguous_multi_gym_owner_which_identity_gate_already_folds_into_unknown():
    ambiguous = ig.resolve(
        "U_MULTI",
        slack_user_info=lambda u: {"email": "multi@x.com", "real_name": "Multi"},
        portal_lookup=lambda e: {
            "role": "owner_none", "gyms": [
                {"gym_id": "g1", "relationship": "client_owner", "account_key": "a1"},
                {"gym_id": "g2", "relationship": "client_owner", "account_key": "a2"},
            ]},
    )
    assert ambiguous.kind == ig.UNKNOWN  # sanity: this is the ambiguous path
    ok, reason = outreach.eligible(_ticket(reporter="multi@x.com", slack_user_id="U_MULTI"),
                                   ambiguous)
    assert ok is False and reason == "identity_unresolved"


def test_refuses_staff_as_the_outreach_recipient_never_staff_filed_on_behalf_of():
    ok, reason = outreach.eligible(_ticket(reporter="blake@lassoframework.com",
                                           slack_user_id="U_STAFF"), _staff())
    assert ok is False and reason == "reporter_is_staff_not_client"


def test_refuses_coach_as_the_outreach_recipient():
    coach = ig.Identity(ig.COACH, "U_COACH", email="coach@gym.com", reason="portal role coach")
    ok, reason = outreach.eligible(_ticket(reporter="coach@gym.com", slack_user_id="U_COACH"),
                                   coach)
    assert ok is False and reason == "reporter_is_staff_not_client"


def test_refuses_when_reporter_does_not_match_the_resolved_client_email_or_slack_id():
    # A ticket "reported by" someone else entirely -- staff filed it ABOUT this client.
    ok, reason = outreach.eligible(
        _ticket(reporter="someone-else@other.com", slack_user_id=""), _client())
    assert ok is False and reason == "reporter_mismatch"


def test_matches_reporter_by_email_case_insensitively():
    ok, reason = outreach.eligible(
        _ticket(reporter="OWNER@GYM.COM", slack_user_id=""), _client())
    assert ok is True and reason == "eligible"


def test_matches_reporter_by_slack_user_id_when_email_is_absent():
    ok, reason = outreach.eligible(
        _ticket(reporter="", slack_user_id="U_CLIENT"), _client())
    assert ok is True and reason == "eligible"


def test_refuses_when_client_identity_has_no_resolved_slack_user_id():
    broken = ig.Identity(ig.CLIENT, "", email="owner@gym.com", account_key="x", gym_id="g-1")
    ok, reason = outreach.eligible(_ticket(), broken)
    assert ok is False and reason == "no_slack_user_id_resolved"


# ---- first message text -----------------------------------------------------------------

def test_first_message_is_plain_language_with_no_dashes():
    ident = ids.IDENTITIES["wrangler"]
    text = outreach.first_message_text(_ticket(), ident)
    assert "-" not in text
    assert "–" not in text and "—" not in text
    assert "Wrangler" in text
    assert "my page shows the wrong hours" in text


def test_first_message_falls_back_gracefully_with_no_raw_text():
    ident = ids.IDENTITIES["echo"]
    text = outreach.first_message_text(_ticket(raw_text=""), ident)
    assert "Echo" in text
    assert "-" not in text


# ---- initiate(): the full flow, with injected Slack calls --------------------------------

def _calls():
    log = {"opened": [], "posted": [], "recorded": []}

    def open_group_dm(user_ids):
        log["opened"].append(list(user_ids))
        return {"ok": True, "channel_id": "G123"}

    def post_first_message(channel_id, text):
        log["posted"].append((channel_id, text))
        return {"ok": True, "ts": "9999.1"}

    def record_outbound(**kwargs):
        log["recorded"].append(kwargs)
        return {"id": "m-1"}

    return log, open_group_dm, post_first_message, record_outbound


def test_initiate_opens_a_group_dm_with_blake_and_the_client_never_a_1to1():
    log, open_dm, post, record = _calls()
    ident = ids.IDENTITIES["wrangler"]
    result = outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                               post_first_message=post, record_outbound=record)
    assert result.opened is True
    assert result.channel_id == "G123"
    assert log["opened"] == [[outreach.BLAKE_SLACK_USER_ID, "U_CLIENT"]]
    assert len(log["opened"][0]) == 2  # never a 1:1 with just the client


def test_initiate_records_the_row_before_posting_row_first():
    log, open_dm, post, record = _calls()
    ident = ids.IDENTITIES["wrangler"]
    outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                      post_first_message=post, record_outbound=record)
    assert len(log["recorded"]) == 1
    assert len(log["posted"]) == 1
    # record_outbound is called for the SAME ticket/body as the post.
    assert log["recorded"][0]["ticket_id"] == "t-1"
    assert log["posted"][0][1] == log["recorded"][0]["body"]


def test_initiate_refuses_and_never_calls_slack_for_an_ineligible_ticket():
    log, open_dm, post, record = _calls()
    ident = ids.IDENTITIES["wrangler"]
    result = outreach.initiate(_ticket(), _unknown(), ident, open_group_dm=open_dm,
                               post_first_message=post, record_outbound=record)
    assert result.opened is False
    assert result.reason == "identity_unresolved"
    assert log["opened"] == []
    assert log["posted"] == []
    assert log["recorded"] == []


def test_initiate_refuses_for_a_staff_filed_ticket_never_calls_slack():
    log, open_dm, post, record = _calls()
    ident = ids.IDENTITIES["scout"]
    result = outreach.initiate(_ticket(reporter="blake@lassoframework.com"), _staff(),
                               ident, open_group_dm=open_dm, post_first_message=post,
                               record_outbound=record)
    assert result.opened is False
    assert log["opened"] == []


def test_initiate_reports_open_failed_without_recording_or_posting():
    log, _open, post, record = _calls()

    def failing_open(user_ids):
        return {"ok": False}

    ident = ids.IDENTITIES["wrangler"]
    result = outreach.initiate(_ticket(), _client(), ident, open_group_dm=failing_open,
                               post_first_message=post, record_outbound=record)
    assert result.opened is False
    assert result.reason == "open_failed"
    assert log["recorded"] == []
    assert log["posted"] == []


def test_initiate_stamps_the_ticket_so_the_dm_becomes_the_ticket_thread():
    log, open_dm, post, record = _calls()
    stamps = []

    def stamp_ticket(ticket_id, **kwargs):
        stamps.append((ticket_id, kwargs))

    ident = ids.IDENTITIES["wrangler"]
    outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                      post_first_message=post, record_outbound=record,
                      stamp_ticket=stamp_ticket)
    assert len(stamps) == 1
    tid, kwargs = stamps[0]
    assert tid == "t-1"
    assert kwargs["channel_id"] == "G123"
    assert kwargs["slack_user_id"] == "U_CLIENT"
    assert kwargs["bot_identity"] == "wrangler"


def test_initiate_never_stamps_when_the_post_fails():
    log, open_dm, _post, record = _calls()
    stamps = []

    def stamp_ticket(ticket_id, **kwargs):
        stamps.append((ticket_id, kwargs))

    def failing_post(channel_id, text):
        return {"ok": False}

    ident = ids.IDENTITIES["wrangler"]
    outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                      post_first_message=failing_post, record_outbound=record,
                      stamp_ticket=stamp_ticket)
    assert stamps == []


def test_initiate_survives_a_stamp_ticket_failure_the_dm_was_already_sent():
    log, open_dm, post, record = _calls()

    def failing_stamp(ticket_id, **kwargs):
        raise RuntimeError("boom")

    ident = ids.IDENTITIES["wrangler"]
    result = outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                               post_first_message=post, record_outbound=record,
                               stamp_ticket=failing_stamp)
    assert result.opened is True
    assert result.reason == "ok"


def test_initiate_still_reports_opened_true_if_the_post_itself_fails():
    log, open_dm, _post, record = _calls()

    def failing_post(channel_id, text):
        return {"ok": False}

    ident = ids.IDENTITIES["wrangler"]
    result = outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                               post_first_message=failing_post, record_outbound=record)
    assert result.opened is True
    assert result.reason == "post_failed"
    # The row was still recorded (row-first) even though the live post failed.
    assert len(log["recorded"]) == 1


# ---- D41/D42 fixes, Frame 1/2 audit wave -------------------------------------------------

def test_first_message_text_escapes_slack_markup_in_the_untrusted_raw_text():
    ident = ids.IDENTITIES["wrangler"]
    ticket = _ticket(raw_text="<!channel> click <http://evil.example|here> to fix it")
    text = outreach.first_message_text(ticket, ident)
    assert "<!channel>" not in text
    assert "<http://evil.example|here>" not in text
    assert "&lt;!channel&gt;" in text
    assert "&lt;http://evil.example|here&gt;" in text


def test_initiate_escapes_a_caller_supplied_message_text_too():
    """The escaping must not be first_message_text()-only -- a caller-supplied
    message_text is untrusted the same way."""
    log, open_dm, post, record = _calls()
    ident = ids.IDENTITIES["wrangler"]
    outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                      post_first_message=post, record_outbound=record,
                      message_text="<@U_EVIL> pay now")
    assert log["posted"][0][1] == "&lt;@U_EVIL&gt; pay now"
    assert log["recorded"][0]["body"] == "&lt;@U_EVIL&gt; pay now"


def test_eligible_refuses_a_ticket_that_was_already_outreached():
    """D41 idempotency: a stamped slack_channel_id means outreach already happened --
    a retry, a re-queued job, or a re-fired caller can never open or post again."""
    ticket = _ticket(slack_channel_id="G_ALREADY")
    ok, reason = outreach.eligible(ticket, _client())
    assert ok is False and reason == "already_outreached"


def test_initiate_marks_the_row_posted_with_the_real_slack_ts_on_success():
    """D41 CRITICAL fix: a row left in 'ready' forever is exactly what lets an armed
    outbox re-post the same first message a second time. mark_message must be called
    with 'posted' and the real ts on a successful post."""
    log, open_dm, post, record = _calls()
    marks = []

    def mark_message(message_id, delivery_status, slack_ts=None):
        marks.append((message_id, delivery_status, slack_ts))

    ident = ids.IDENTITIES["wrangler"]
    result = outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                               post_first_message=post, record_outbound=record,
                               mark_message=mark_message)
    assert result.opened is True and result.reason == "ok"
    assert marks == [("m-1", "posted", "9999.1")]


def test_initiate_marks_the_row_failed_never_leaves_it_ready_when_the_post_fails():
    log, open_dm, _post, record = _calls()
    marks = []

    def mark_message(message_id, delivery_status, slack_ts=None):
        marks.append((message_id, delivery_status, slack_ts))

    def failing_post(channel_id, text):
        return {"ok": False}

    ident = ids.IDENTITIES["wrangler"]
    result = outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                               post_first_message=failing_post, record_outbound=record,
                               mark_message=mark_message)
    assert result.reason == "post_failed"
    assert marks == [("m-1", "failed", None)]


def test_initiate_without_mark_message_injected_still_works_backward_compatible():
    """mark_message is optional so existing/dry-run callers keep working; the row-
    lifecycle fix is additive, not a breaking change to the injected-deps shape."""
    log, open_dm, post, record = _calls()
    ident = ids.IDENTITIES["wrangler"]
    result = outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                               post_first_message=post, record_outbound=record)
    assert result.opened is True and result.reason == "ok"


def test_eligible_refuses_an_unverified_reporter_even_when_everything_else_matches():
    """D42 CRITICAL fix: a ticket.reporter matching who's email/slack_user_id only
    proves internal consistency, not that the intake submitter owns that identity.
    No current NON_SLACK_SOURCES producer can set reporter_verified, so this fails
    closed for everyone until a real producer positively asserts provenance."""
    ticket = _ticket(reporter_verified=False)
    ok, reason = outreach.eligible(ticket, _client())
    assert ok is False and reason == "reporter_not_verified"


def test_eligible_refuses_when_reporter_verified_key_is_entirely_absent():
    ticket = _ticket()
    del ticket["reporter_verified"]
    ok, reason = outreach.eligible(ticket, _client())
    assert ok is False and reason == "reporter_not_verified"


# ---- closing-audit fixes (fresh independent verifier of the D41/D42 fix commit) --------

def test_eligible_refuses_any_truthy_but_not_literal_true_reporter_verified_value():
    """A future producer setting reporter_verified to a token string, a timestamp, or
    any other truthy-but-not-boolean value must NOT accidentally satisfy the gate --
    the whole point of D42 is that only the literal boolean True, set deliberately,
    opens it."""
    for bad_value in ("True", "yes", 1, "pending-not-really", object(), "false"):
        ticket = _ticket(reporter_verified=bad_value)
        ok, reason = outreach.eligible(ticket, _client())
        assert ok is False and reason == "reporter_not_verified", (
            f"reporter_verified={bad_value!r} must not pass the gate")


def test_initiate_does_not_double_escape_the_default_first_message_text():
    """Closing-audit finding: initiate() used to re-escape first_message_text()'s
    already-escaped output, turning '&lt;' into '&amp;lt;'. The default (message_text=
    None) path must post text escaped exactly once."""
    log, open_dm, post, record = _calls()
    ident = ids.IDENTITIES["wrangler"]
    ticket = _ticket(raw_text="<!channel> urgent")
    outreach.initiate(ticket, _client(), ident, open_group_dm=open_dm,
                      post_first_message=post, record_outbound=record)
    posted_text = log["posted"][0][1]
    assert "&lt;!channel&gt;" in posted_text
    assert "&amp;lt;" not in posted_text
    assert "&amp;amp;" not in posted_text


def test_initiate_claims_the_row_before_posting_when_claim_message_is_injected():
    """D44 (MINOR, Frame 2 closing-audit finding): initiate() posted directly without
    ever claiming the row, leaving it in 'ready' for the whole duration of the post --
    the exact window a concurrently-armed outbox loop could also see it. claim_message
    (the same ready->posting CAS the outbox itself uses) is now called right after the
    row is written and before the post, closing that window."""
    log, open_dm, post, record = _calls()
    claims = []

    def claim_message(message_id):
        claims.append(message_id)
        return True

    ident = ids.IDENTITIES["wrangler"]
    result = outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                               post_first_message=post, record_outbound=record,
                               claim_message=claim_message)
    assert result.opened is True and result.reason == "ok"
    assert claims == ["m-1"]


def test_initiate_backs_off_without_posting_when_the_claim_is_lost():
    log, open_dm, post, record = _calls()

    def losing_claim(message_id):
        return False

    ident = ids.IDENTITIES["wrangler"]
    result = outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                               post_first_message=post, record_outbound=record,
                               claim_message=losing_claim)
    assert result.reason == "lost_claim"
    assert log["posted"] == [], "a lost claim must never still post -- another consumer has this row"


def test_initiate_without_claim_message_injected_still_works_backward_compatible():
    log, open_dm, post, record = _calls()
    ident = ids.IDENTITIES["wrangler"]
    result = outreach.initiate(_ticket(), _client(), ident, open_group_dm=open_dm,
                               post_first_message=post, record_outbound=record)
    assert result.opened is True and result.reason == "ok"
