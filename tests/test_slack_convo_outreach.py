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
