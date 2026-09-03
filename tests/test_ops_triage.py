"""
ops_triage.py tests. Every NOISE assertion below is a REAL alert line Blake was shown on
2026-09-02 (three screenshots of #echoclaude, 12:01 AM - 8:51 AM) -- the ground truth for
what an operator was fine ignoring. Every NEEDS_TRIAGE assertion is either a real line from
that same batch that genuinely needed a look, or a synthetic case pinning the fail-safe
default (an alert shape this module has never seen must default to needs_triage, never
noise).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import ops_triage as ot  # noqa: E402


# ---- real NOISE lines, 2026-09-02 -------------------------------------------------------

def test_calendar_auto_published_is_noise():
    assert ot.classify(
        "Calendar auto-published (4): lasso_fb, lasso_ig | 2026-09-02") == ot.NOISE


def test_re_dated_expired_rows_no_action_needed_is_noise():
    assert ot.classify(
        "ECHO ALERT: gritx: re-dated 4 expired row(s) into open day(s) "
        "2026-10-01..2026-10-02 (approvals preserved). No action needed."
    ) == ot.NOISE


def test_out_of_fresh_creative_not_blocked_is_noise():
    assert ot.classify(
        "ECHO ALERT: swiftrivercrossfite5c9db is out of fresh creative: only 9 usable "
        "photo(s)/video(s) for a 30-day month, so the calendar is capped at 9 post(s) and "
        "recycles them. Ask the gym for more material to fill the month. Not blocked; the "
        "current calendar stands."
    ) == ot.NOISE


def test_held_as_needs_media_not_blocked_is_noise():
    assert ot.classify(
        "ECHO ALERT: topfuel_ig: 1 day(s) held as needs-media (2026-10-08). Captions are "
        "ready but the library has no image. Add photos (connect the gym's Drive folder or "
        "upload in the portal) to publish. Not blocked; slots fill as media arrives."
    ) == ot.NOISE


def test_gbp_address_unparseable_no_action_is_noise():
    assert ot.classify(
        "ECHO ALERT: GBP conn sync: new swiftrivercrossfite5c9db connection address was "
        "unparseable ('64 Hobbs Street #3, Conway, New Hampshire'); tz set to "
        "America/Indianapolis (the gym's own posting tz when known). No action needed "
        "unless posts land at an odd hour."
    ) == ot.NOISE


def test_register_gym_refused_duplicate_no_action_is_noise():
    assert ot.classify(
        "ECHO ALERT: register_gym: refused to register 'crossfitnine782f21b' as a NEW "
        "gym -- gym_id 82f21b3c-4111-47f7-a7c3-ba82eb6a2b7c is already registered under "
        "'crossfitnine7f7dadc'. Returned the existing base's keys instead of forking the "
        "registry (this is the structural guard for the Sunnyside/Swift River split-key "
        "class; no action needed unless these two bases are genuinely different gyms)."
    ) == ot.NOISE


def test_podcast_index_summary_is_noise():
    assert ot.classify(
        "podcast index: 141 episodes seen, 40 clips + 139 audiograms found, 0 new, 0 "
        "updated, 0 probed, 0 newly postable, rejected: none, 0 removed_from_drive, 743 "
        "unclassifiable skipped, 0 denied clip(s) returned to pool"
    ) == ot.NOISE


def test_new_team_media_synced_is_noise():
    assert ot.classify(
        "New team media synced for train7164ae502: 111 new (111 photos, 0 videos this "
        "scan), 0 newly ready to use, rejected: none. Review or hide any in the portal "
        "media tab."
    ) == ot.NOISE


def test_grade_sweep_rollup_is_noise():
    assert ot.classify(
        "ECHO ALERT: grade sweep: 19 gyms, 1 self-fixed to A, 7 held (lasso, topfuel, "
        "piercefitness, hillcountry, zanshinfitness630e22, crossfitreverb30b5b2, "
        "crossfitnine7f7dadc)"
    ) == ot.NOISE


def test_grade_held_at_b_after_self_fix_is_noise():
    assert ot.classify(
        "ECHO ALERT: calendar grade: hillcountry forward book held at 89 (B) after "
        "self-fix.\nAuto-fixed: nothing auto-fixable.\nRemaining: ['community is 34% of "
        "posts (over 25%)', 'faces is 31% of posts (over 25%)']"
    ) == ot.NOISE


def test_grade_held_at_c_after_self_fix_is_noise():
    assert ot.classify(
        "ECHO ALERT: calendar grade: piercefitness forward book held at 78 (C) after "
        "self-fix.\nAuto-fixed: nothing auto-fixable.\nRemaining: ['about is 35% of posts "
        "(over 25%)', 'soft flag: hook_too_long', 'soft flag: no_ask']"
    ) == ot.NOISE


# ---- real NEEDS_TRIAGE lines, 2026-09-02 -------------------------------------------------

def test_intake_ingest_aborted_is_needs_triage():
    assert ot.classify(
        "ECHO ALERT: intake ingest ABORTED for piercefitness (other gyms unaffected): "
        "ClientError: An error occurred (InternalError) when calling the PutObject "
        "operation (reached max retries: 4): We encountered an internal error. Please "
        "try again."
    ) == ot.NEEDS_TRIAGE


def test_key_mismatch_is_needs_triage():
    assert ot.classify(
        "ECHO ALERT: crossfitnine782f21b: not set up to post (key_mismatch, no_sources, "
        "no_profile). its intake forwarded under a DIFFERENT account key, so its answers "
        "landed where nothing reads them. Migrate the sources onto the portal key."
    ) == ot.NEEDS_TRIAGE


def test_no_fb_page_is_needs_triage():
    assert ot.classify(
        "ECHO ALERT: theboltonclub: not set up to post (no_fb_page). Facebook is "
        "connected but no PAGE is selected, so every Facebook publish raises 'no Facebook "
        "page selected'. Stamp zernio_default_fb_page_id from the account's "
        "metadata.selectedPageId."
    ) == ot.NEEDS_TRIAGE


def test_not_connected_is_needs_triage():
    assert ot.classify(
        "ECHO ALERT: crossfitnewtown: not set up to post (not_connected). a Zernio "
        "profile exists but ZERO platforms are connected. Send the gym its connect link "
        "(python -m agent intake-link --account <key>)."
    ) == ot.NEEDS_TRIAGE


def test_grade_gate_not_staging_is_needs_triage():
    assert ot.classify(
        "ECHO ALERT: calendar grade gate: lasso scored 89 (B) after 4 remediation "
        "passes. Top defects: ['doctrine is 91% of posts (over 25%)', 'only 0/8 captions "
        "contain a @mention']. NOT STAGING — human decision needed."
    ) == ot.NEEDS_TRIAGE


def test_grade_dropped_is_needs_triage_even_though_wording_resembles_held():
    assert ot.classify(
        "ECHO ALERT: calendar grade DROPPED: lasso forward book went 43 -> 40 (F) since "
        "the last run.\nA drop means new defects are being BUILT into the book faster "
        "than the nightly repair clears them.\nTop defects now: ['gap of 8 days before "
        "2026-10-12', 'gap of 1 day before 2026-10-20', 'gap of 1 day before 2026-10-27']"
    ) == ot.NEEDS_TRIAGE


def test_grade_held_at_f_is_needs_triage_not_noise():
    # same "held at ... after self-fix" shape as the noise cases above, but an F means
    # the nightly repair genuinely cannot clear it -- must NOT be suppressed.
    assert ot.classify(
        "ECHO ALERT: calendar grade: lasso forward book held at 40 (F) after self-fix.\n"
        "Auto-fixed: nothing auto-fixable.\nRemaining: ['gap of 8 days before 2026-10-12']"
    ) == ot.NEEDS_TRIAGE


def test_key_mismatch_registration_alert_is_needs_triage():
    # the sibling of the noise-classified duplicate-registration guard: THIS one is the
    # new gym's own key-mismatch alert, not the "no action needed" guard line.
    assert ot.classify(
        "ECHO ALERT: mflha0fcb1: not set up to post (key_mismatch, no_sources, "
        "no_profile). its intake forwarded under a DIFFERENT account key, so its answers "
        "landed where nothing reads them. Migrate the sources onto the portal key."
    ) == ot.NEEDS_TRIAGE


# ---- fail-safe defaults ------------------------------------------------------------------

def test_unrecognized_shape_defaults_to_needs_triage():
    assert ot.classify("something ops_alerts.alert() has never said before") == ot.NEEDS_TRIAGE


def test_empty_and_none_default_to_needs_triage_never_crash():
    assert ot.classify("") == ot.NEEDS_TRIAGE
    assert ot.classify(None) == ot.NEEDS_TRIAGE


def test_a_no_action_phrase_wins_even_inside_an_unfamiliar_alert():
    # the explicit-phrasing convention is trusted broadly, by design (see module
    # docstring) -- any alert author who writes it means it.
    assert ot.classify("ECHO ALERT: some brand new check failed weirdly. No action "
                       "needed, it retries on its own.") == ot.NOISE


# ---- CLI entry point ----------------------------------------------------------------------

def test_main_classifies_an_arg(capsys):
    ot.main(["Calendar auto-published (1): lasso_fb | 2026-09-02"])
    assert capsys.readouterr().out.strip() == "noise"


def test_main_classifies_stdin(capsys, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("ECHO ALERT: intake ingest ABORTED"))
    ot.main([])
    assert capsys.readouterr().out.strip() == "needs_triage"


# ---- systemic detection (2026-09-02 storm) ---------------------------------------------
# Supabase's REST layer wedged and EVERY gym raised 'calendar_unreadable' at once. Each
# alert cross-posted an ops-fix request, each request spawned a Claude Code session that
# ran live database diagnostics against the database that was already down. Seven sessions
# for one cause, accelerating. A shared-dependency failure is ONE incident.

def test_the_real_stall_alerts_from_the_storm_are_systemic():
    for gym in ("district_h", "topfuel", "piercefitness", "theboltonclub",
                "crossfitlocal", "hillcountry", "train7164ae502"):
        msg = (f"ECHO ALERT: gym {gym} is STALLED at 'calendar_unreadable': the shared "
               "calendar could not be read (Supabase creds/network); no rebuild will run "
               "until it reads again — its content pipeline cannot advance until a human "
               "fixes this.")
        assert ot.is_systemic(msg) is True, gym
        # still needs a human: systemic never means silent
        assert ot.classify(msg) == ot.NEEDS_TRIAGE, gym


def test_the_real_readtimeout_alert_is_systemic():
    msg = ("ECHO ALERT: GBP lane failed: ReadTimeout: HTTPSConnectionPool("
           "host='ooqcvmcjspeltuuhcvlh.supabase.co', port=443): Read timed out. "
           "(read timeout=30). The draft run is unaffected.")
    assert ot.is_systemic(msg) is True


def test_a_single_gym_content_problem_is_NOT_systemic():
    """The distinction that matters: one gym's own content defect must still fan out
    normally, or collapsing would hide real per-gym work."""
    for msg in (
        "ECHO ALERT: theboltonclub: not set up to post (no_fb_page). Facebook is connected "
        "but no PAGE is selected.",
        "ECHO ALERT: crossfitnine782f21b: not set up to post (key_mismatch, no_sources).",
        "ECHO ALERT: calendar grade DROPPED: lasso forward book went 43 -> 40 (F).",
        "ECHO ALERT: publish guard: row f75c19e9 (gym lasso) blocked at the publish "
        "boundary (multi_ask); reverted to pending with reject_reason.",
    ):
        assert ot.is_systemic(msg) is False, msg[:60]


def test_is_systemic_is_prefix_and_case_tolerant_and_never_raises():
    bare = ("gym x is STALLED at 'calendar_unreadable': the shared calendar could not be "
            "read (Supabase creds/network)")
    assert ot.is_systemic(bare) is True
    assert ot.is_systemic(bare.upper()) is True
    assert ot.is_systemic("") is False
    assert ot.is_systemic(None) is False
