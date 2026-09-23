"""
outbox.py — the Wrangler outbound role. The ONLY code in this package that posts to Slack.

Blake (spec item 8): "Wrangler owns all outbound. The adapter never calls chat.postMessage
itself. It writes the row, Wrangler posts." Ground truth 2026-09-03: no Wrangler process
reads support_messages today, so this in-process loop IS the outbound role for now. Its
whole interface is `run_once(bus, post, ...)`; when a real Wrangler service exists, it takes
over by pointing at the same rows and this loop is disabled by config. Nothing else in the
package would change.

It reads rows in delivery_status='ready' and, for each, re-checks EVERY gate at post time
(not only at write time -- a flag can be flipped between the two):

  0. KNOWN KIND, KNOWN IDENTITY. A row whose kind is not one of ours, or that carries no
     identity stamp, is suppressed, not posted (V-M7: fail closed).
  1. FIRST CONTACT. The parent ticket must carry at least one inbound human row, or the row
     is suppressed. The bot never speaks first, structurally.
  2. VERIFICATION. A substantive reply (kind=answer) posts only if the parent ticket has
     verification_after populated. Missing -> suppressed, loudly. Never "should be fixed."
  3. NO RE-ENTRY. A conversational body carrying the ops-fix trigger prefix is suppressed;
     the bot's own reply must never be readable as a command by the ops-fix worker (RT-m2).
  4. FRESHNESS. A conversational row that sat in 'ready' longer than STALE_AFTER_SECONDS
     (outbox down, channel unset) is suppressed: a stale "checking that now" hours later is
     worse than silence (V-m2). Internal rows never go stale; a human still needs them.
  5. TRUST LADDER. A conversational reply to a client posts only if the identity's
     client-reply flag is armed; to staff only if the staff flag is armed. Otherwise the row
     is moved to 'held' AND a tap notice is written (V-M8), so nothing sits held unseen. A
     row Blake has explicitly released (release_held stamped released_by) skips this recheck
     once -- his tap IS the approval the trust ladder exists to collect (N2).
  6. CLAIM. The row is moved ready/held->posting with a conditional PATCH only one caller
     can win, immediately before post() (N4). This is what makes two consumers of the same
     row safe.
  7. DESTINATION. Internal kinds (escalation, hold_notice) go to the fixer channel and
     fixer_request goes to the ops-fix intake channel the existing worker watches. They
     never enter the person's thread. Conversational kinds go to the ticket's own channel:
     top-level in a DM or group DM (people do not thread there), in-thread for a mention or
     a channel thread.

Every SUPPRESSION writes an escalation row, so a human sees what the bot declined to say
(V-M5). A hold notice posts with a Block Kit button (action id slack_convo_release, value =
the held row id) so the tap actually exists (V-M2 / RT-m5), rendered across as many sections
as the full body needs (RA-M2: Blake must review exactly the text that will post on release,
never a truncated prefix of it). When an answer posts, its ticket is marked resolved -- the
ticket closes when the person has the answer, not before (V-M4).

A post failure marks the row 'failed' and moves on; one bad row never stalls the queue.

HARDENING (2026-09-03 re-audit wave 2):
  N2  Blake's tap on a held row used to be swallowed silently: release_held flipped it to
      'ready', but gate 5 re-read the SAME flag that had held it, found it still off (that
      IS why it was held), and held it again -- writing a fresh card each time, forever. A
      released row now skips the trust-ladder recheck once.
  N4  Read-then-post-then-mark had no claim step: two consumers (a redeploy overlap, a
      second Wrangler pointed at the same rows per D2) could post the same row twice, and a
      mark_message failure after a successful post left the row in 'ready' to be reposted
      forever. Every row is now claimed (ready/held -> posting, a conditional PATCH only one
      caller can win) immediately before post(); any row still in 'posting' at the START of
      a run_once call is orphaned from a crashed prior attempt (claim -> post -> mark is
      synchronous within one call, so nothing legitimate is ever mid-flight across calls)
      and is swept back to 'ready'.
  RA-M2  hold_notice_blocks used to show only the first 2900 characters while release posted
      the full row -- an injected tail could sit invisible to the reviewer. Now it renders
      as many sections as the body needs; what Blake reviews is what posts.
  RA-m5  fixer_request always used the shared ops-fix worker channel (ground truth: only one
      worker exists, and it only trusts Echo's bot_id -- a cross-identity ruling for Blake,
      D10). escalation / hold_notice now honour the identity's OWN fixer_channel_env when
      set, instead of always the global default, so a second identity's holds do not land
      in Echo's channel.
"""
from datetime import datetime, timezone
import hashlib
import json
import re

from . import adapter as _a
from .. import config

# Surfaces where a reply goes TOP LEVEL rather than in a thread: DMs and group DMs (people do
# not thread there), and a portal-bridge ticket, whose Slack home is the group DM this system
# opened for it.
TOP_LEVEL_SURFACES = frozenset({"im", "mpim", "portal_ticket_bridge"})

STALE_AFTER_SECONDS = 6 * 3600
RELEASE_ACTION_ID = "slack_convo_release"
RESOLVE_ACTION_ID = "slack_convo_resolve"
_REENTRY_PREFIX = "OPS-FIX REQUEST"
_BLOCK_TEXT_CHARS = 2900

# D48 (Blake, 2026-09-05): a ticket the person submitted IN THE PORTAL has a second, real
# delivery surface that is not Slack -- the /my/support/[ticketId] thread they submitted it
# from. Migration 0310 already decides what a client may read there: an outbound row that is
# delivery_status='posted' and not an internal kind. So for these tickets "post" means
# "release the row into the thread they are already looking at", and gate 7 below marks it
# posted instead of failing it for having no Slack channel. Restricted to the two sources a
# client actually submits through the portal UI (a website_intake / engage_tenant_event
# ticket has no portal reader), and to tickets carrying a client_id, without which 0310's
# own predicate can never match the reader to the row.
PORTAL_THREAD_SOURCES = frozenset({"portal_form", "website_tab"})

RESOLVED_NOTICE = (
    "Update from the LASSO team: this one is handled. If that is not what you needed, "
    "reply here and we will pick it back up.")


def portal_deliverable(ticket):
    """True when the portal support thread is a real delivery surface for this ticket."""
    t = ticket or {}
    return (str(t.get("source") or "") in PORTAL_THREAD_SOURCES
            and bool(str(t.get("client_id") or "").strip()))


def _direct_answerable_question(ticket, body=""):
    """A grounded question answer is not a code-fix completion.

    It may skip deployment proof, but FIXER-authored rows are separately bound to
    the current durable requester hash below. Keep this predicate independent of
    authorship so ordinary Echo answers retain their existing behavior.
    """
    t = ticket or {}
    classification = str(t.get("classification") or "").lower()
    direct_question = (classification == "answerable_question"
                       and t.get("status") == "verification"
                       and t.get("escalated") is not True
                       and not t.get("hold_tier")
                       and not (t.get("verification_after") or {}).get("hold"))
    promised_work = (_a.answer_commits_to_action(str(body or ""))
                     or _a.promises_human_follow_up(str(body or "")))
    return direct_question and not promised_work


def _fixer_grounded_question_answer(ticket, att, kind, body=""):
    """True only for the narrow FIXER question-answer deployment exemption."""
    return (kind == _a.KIND_ANSWER
            and bool((att or {}).get("fixer"))
            and _direct_answerable_question(ticket, body))


def _customer_fix_reply(ticket, att, body=""):
    """Identify customer handoffs even after escalation clears classification.

    The FIXER poll requires classification NULL, so classification alone cannot
    protect held portal incidents. A direct grounded QUESTION keeps its explicit
    classification and remains eligible for the normal answer gates.
    """
    recipient = (att.get("recipient_kind") or ticket.get("identity_kind") or "client")
    if recipient in ("staff", "coach"):
        return False
    # A grounded answer remains an answer when Scout/FIXER authored it.  The
    # normal answer gates below still re-run the hard-line verdict, arming, and
    # conversation checks. Treating every `fixer: true` row as a code-fix
    # completion sent it into the deployment gate, where it could never pass
    # because an answer has no PR or release evidence.
    if _direct_answerable_question(ticket, body):
        return False
    classification = str(ticket.get("classification") or "").lower()
    portal_handoff = (ticket.get("product") == "echo"
                      and portal_deliverable(ticket)
                      and (ticket.get("escalated") is True
                           or bool(ticket.get("hold_tier"))
                           or bool((ticket.get("verification_after") or {}).get("hold"))))
    return classification == "code_fix" or bool(att.get("fixer")) or portal_handoff


def _swap_siblings_verified(result, row_id):
    """Sibling evidence parity for a swap that legitimately moved sibling rows.

    fixer_ops._run_swap_media proves the postcondition by reading back the target row
    AND every sibling from the calendar store before it stamps postcondition_verified,
    so a nonempty siblings_swapped list is a legitimate, verified outcome -- requiring
    it to be empty made such a swap unclosable. Accept it only with the same evidence
    the producer's readback verified: exact unique nonempty ids, sibling_results whose
    entry ids match siblings_swapped one for one (no missing, no extra), each entry
    carrying the readback fields (display url, media kind, and the video pairing rule
    the target row itself is held to), and nothing left behind. The target row's own
    id is never a sibling -- its appearance in either list is a malformed or
    adversarial payload. Any deviation fails closed exactly like an empty-swap record
    that lost its readback."""
    swapped = result.get("siblings_swapped")
    if not isinstance(swapped, list) or result.get("siblings_left") != []:
        return False
    ids = []
    for sid in swapped:
        if not isinstance(sid, str) or not sid.strip() or sid == row_id:
            return False
        ids.append(sid)
    if len(set(ids)) != len(ids):
        return False  # duplicate sibling ids are never verified evidence
    evidence = result.get("sibling_results")
    if evidence is None:
        evidence = []  # legacy empty-swap records predate the key; [] must still pass
    if not isinstance(evidence, list):
        return False
    entries = []
    for entry in evidence:
        if not isinstance(entry, dict):
            return False
        sid = entry.get("id")
        if not isinstance(sid, str) or not sid.strip() or sid == row_id:
            return False
        entries.append(entry)
    if sorted(entry["id"] for entry in entries) != sorted(ids):
        return False
    for entry in entries:
        url = entry.get("image_public_url")
        kind = entry.get("media_kind")
        if not (isinstance(url, str) and url.strip()):
            return False
        if kind == "image":
            if entry.get("video_url") is not None:
                return False
        elif kind == "video":
            video = entry.get("video_url")
            if not (isinstance(video, str) and video.strip()):
                return False
        else:
            return False
    return True


def _count_field(value):
    """An integer count field; a bool is never a count."""
    return isinstance(value, int) and not isinstance(value, bool)


def _release_denied_assets_verified(operation):
    """release_denied_assets closes a customer ticket only on the sweep's
    independently re-proven outcome.

    fixer_ops._run_release_denied_assets stamps postcondition_verified True ONLY
    when its pre-sweep derivation, the sweep's own claim, a post-sweep ledger
    re-read and per-asset counter re-reads all agree -- and a zero-rollback sweep
    verifies nothing by construction, so a genuine verified outcome always carries
    a nonzero rolled_back and at least one probed date. evidence_note exists ONLY
    on an unverified outcome: its presence, a bare verified flag without the
    measured counts, or prose offered as proof all fail closed."""
    result = operation.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("postcondition_verified") is not True or "evidence_note" in result:
        return False
    rolled = result.get("rolled_back")
    checked = result.get("checked")
    captured = result.get("captured_at")
    return (_count_field(rolled) and rolled >= 1
            and _count_field(checked) and checked >= 1
            and isinstance(captured, str) and bool(captured.strip()))


def _restage_month_verified(operation, ticket):
    """restage_month closes a customer ticket only on the background job's
    TERMINAL record, bound to this ticket.

    The 202 job-start payload proves nothing, and a running, timed_out or failed
    job never verifies. fixer_ops.run_restage_month stamps the terminal job
    result's postcondition_verified True ONLY when an independent before/after
    calendar snapshot comparison confirms the build's claimed row writes; the
    builder's own postcondition_verified is stripped on that path, so its
    presence here means the payload did not come from the truthful producer. A
    no-op build verifies nothing, so verified implies upserted >= 1. The job
    record must name this ticket and one gym, its inner result the same gym, and
    both must match the operation's recorded gym_key when one was stamped."""
    job = operation.get("result")
    if not isinstance(job, dict):
        return False
    if (job.get("action") != "restage_month" or job.get("status") != "done"
            or job.get("error") is not None):
        return False
    job_id = job.get("id")
    finished = job.get("finished_at")
    gym = job.get("gym_key")
    if not (isinstance(job_id, str) and job_id.strip()
            and isinstance(finished, str) and finished.strip()
            and isinstance(gym, str) and gym.strip()):
        return False
    if job.get("ticket_id") != (ticket or {}).get("id"):
        return False
    op_gym = operation.get("gym_key")
    if isinstance(op_gym, str) and op_gym.strip() and op_gym != gym:
        return False
    result = job.get("result")
    if not isinstance(result, dict):
        return False
    if (result.get("postcondition_verified") is not True
            or "evidence_note" in result
            or result.get("gym_key") != gym):
        return False
    captured = result.get("captured_at")
    build = result.get("build")
    if not isinstance(build, dict) or build.get("ok") is not True:
        return False
    if "postcondition_verified" in build:
        return False  # the truthful path strips the builder's self-certification
    upserted = build.get("upserted")
    return (_count_field(upserted) and upserted >= 1
            and isinstance(captured, str) and bool(captured.strip()))


# The stamped business_postcondition is a POINTER, never the proof: only its check_id
# and params are used, to re-run the independent observation at dispatch time through
# fixer_business_evidence. Any stamped verified / symptom_resolved / evidence prose is
# untrusted and ignored for the decision.
BUSINESS_EVIDENCE_MAX_AGE_SECONDS = STALE_AFTER_SECONDS
_BUSINESS_EVIDENCE_FUTURE_SKEW_SECONDS = 300
_RELEASE_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _business_evidence_deps(bus, check_id=None):
    """The bounded, read-only reader fixer_business_evidence.observe() requires,
    adapted from the support bus's own bounded PostgREST-style read (the same one
    _person_for_card uses). The registered checks pass their own explicit limit in
    the params and the evidence module rejects over-limit or partial payloads, so
    the adapter stays thin; a bus without that read yields no reader, and no reader
    fails closed at the caller."""
    get = getattr(bus, "_get", None)
    if not callable(get):
        return None

    def read(table, params):
        return get(table, params)

    deps = {"read": read}
    if check_id == "media_swap_completed":
        # The Slack outbox runs on the Echo worker with its durable SQLite
        # volume. Keyed FIXER swaps intended for automatic closure must run on
        # that same worker. A receipt written on intake-web's separate volume
        # will be absent here and the observer will fail closed.
        from .. import fixer_ops_receipts as receipts
        store = receipts.default_store()
        deps["receipt_read"] = lambda key, echo_key: receipts.get_receipt(
            store, key, echo_key)
    return deps


def _business_postcondition_observed(bus, ticket, business, merged_sha, now=None):
    """The dispatch-time authoritative read behind the business_postcondition branch.

    The stamped record supplies ONLY the check_id and params to re-run; the verdict
    comes from a fresh fixer_business_evidence.observe() through a bounded read-only
    reader, bound to this ticket's tenant, the CURRENT recomputed request key, and
    the exact release SHA. Any missing pointer field, unavailable reader, stale or
    future observation, non-VERIFIED outcome, or binding mismatch fails closed --
    an inline stamped dict can never close the ticket by itself."""
    check_id = business.get("check_id")
    params = business.get("params")
    if not (isinstance(check_id, str) and check_id.strip()
            and isinstance(params, dict)):
        return False  # a pointer without a check id and params points at nothing
    gym_key = (ticket or {}).get("client_id")
    if not (isinstance(gym_key, str) and gym_key.strip()):
        return False
    if bus is None:
        return False  # no reader can be constructed: fail closed, never skip
    try:
        current_key = _current_fixer_request_key(bus, ticket)
    except Exception:  # noqa: BLE001 - an unreadable thread cannot prove the request
        return False
    if not current_key:
        return False
    deps = _business_evidence_deps(bus, check_id)
    if deps is None:
        return False
    try:
        from .. import fixer_business_evidence as _fbe
        observed = _fbe.observe(check_id, gym_key=gym_key, request_key=current_key,
                                merged_sha=merged_sha, params=params, deps=deps,
                                ticket_id=str(ticket.get("id") or ""), now=now)
    except Exception:  # noqa: BLE001 - an observer fault is not evidence
        return False
    if not isinstance(observed, dict):
        return False
    captured = _parse_ts(observed.get("captured_at"))
    ref = now or datetime.now(timezone.utc)
    age = (ref - captured).total_seconds() if captured is not None else None
    return (observed.get("schema_version") == 1
            and observed.get("outcome") == _fbe.VERIFIED
            and observed.get("verified") is True
            and observed.get("symptom_resolved") is True
            and age is not None
            and -_BUSINESS_EVIDENCE_FUTURE_SKEW_SECONDS <= age
            <= BUSINESS_EVIDENCE_MAX_AGE_SECONDS
            and _fbe.binding_matches(observed, gym_key=gym_key,
                                     request_key=current_key, merged_sha=merged_sha))


def _verified_fix_notice(ticket, att, kind, *, bus=None, now=None):
    """Only the current fix's resolve notice may tell a customer it is handled.

    Most ops_action branches retain their existing payload gates. A swap_media
    resolution also requires the same durable business observation as the code
    fix branch; its handler result alone cannot prove the photo changed."""
    verification = ticket.get("verification_after") or {}
    release = verification.get("fixer") or {}
    deployment = release.get("deployment_check") or {}
    if kind != _a.KIND_STATUS or att.get("resolve_notice") is not True:
        return False
    operation = release.get("ops_action") or {}
    if att.get("ops_action") in {"resend_connect_link", "reset_recreate_budget",
                                 "requeue_failed_row", "swap_media",
                                 "release_denied_assets", "restage_month"}:
        common = (ticket.get("status") == "verification"
                and release.get("postcondition_verified") is True
                and operation.get("identityVerified") is True
                and operation.get("ok") is True
                and operation.get("action") == att.get("ops_action")
                and operation.get("tenantVerified") is True
                and bool(ticket.get("client_id"))
                and operation.get("tenantId") == ticket.get("client_id")
                and bool(release.get("request_key")))
        if not common:
            return False
        action = att.get("ops_action")
        if action == "release_denied_assets":
            return _release_denied_assets_verified(operation)
        if action == "restage_month":
            return _restage_month_verified(operation, ticket)
        if action != "swap_media":
            return True
        args = operation.get("args") or {}
        result = operation.get("result") or {}
        if not isinstance(args, dict) or not isinstance(result, dict):
            return False
        row_id = args.get("row_id")
        media_url = result.get("image_public_url")
        media_kind = result.get("media_kind")
        payload_ok = (isinstance(row_id, str) and bool(row_id.strip())
                and result.get("ok") is True
                and result.get("action") == "swap-media"
                and result.get("postcondition_verified") is True
                and result.get("draft_id") == row_id
                and _swap_siblings_verified(result, row_id)
                and isinstance(media_url, str) and bool(media_url.strip())
                and (media_kind == "image" and result.get("video_url") is None
                     or media_kind == "video" and
                     isinstance(result.get("video_url"), str) and
                     bool(result["video_url"].strip())))
        if not payload_ok:
            return False
        business = release.get("business_postcondition") or {}
        merged_sha = release.get("merged_sha")
        if (not isinstance(business, dict)
                or business.get("check_id") != "media_swap_completed"
                or not isinstance(business.get("params"), dict)
                or business["params"].get("row_id") != row_id
                or not isinstance(merged_sha, str)
                or not _RELEASE_SHA.fullmatch(merged_sha)):
            return False
        return _business_postcondition_observed(bus, ticket, business, merged_sha,
                                                now=now)
    # A healthy deployment proves the code is live, not that this owner's symptom
    # is gone. The stamped business_postcondition is only a pointer to the check that
    # must be re-run at dispatch time; its stamped verdict and prose are untrusted.
    business = release.get("business_postcondition") or {}
    if not isinstance(business, dict):
        return False
    merged_sha = release.get("merged_sha")
    if not (kind == _a.KIND_STATUS and att.get("resolve_notice") is True
            and ticket.get("status") == "merged"
            and verification.get("exit_code") == 0
            and verification.get("incomplete") is not True
            and bool(ticket.get("fix_pr_url"))
            and att.get("pr_url") == ticket.get("fix_pr_url")
            and isinstance(merged_sha, str) and _RELEASE_SHA.fullmatch(merged_sha)
            and deployment.get("verified") is True
            and deployment.get("sha") == merged_sha
            and bool(release.get("request_key"))):
        return False
    return _business_postcondition_observed(bus, ticket, business, merged_sha, now=now)


def _current_fixer_request_key(bus, ticket):
    """Recompute Scout's requestKey from all requester inbound rows, failing closed.

    A truncated message read cannot prove the current request. Require fewer
    than the normal PostgREST response cap and compare a separate inbound read.
    """
    rows = bus.messages(ticket["id"], limit=1000)
    if not isinstance(rows, list) or len(rows) >= 1000:
        return None
    inbound = [m for m in rows if m.get("direction") == "inbound"]
    if len(inbound) != bus.inbound_count(ticket["id"]):
        return None
    requester = []
    for m in inbound:
        att = m.get("attachments") or {}
        author = m.get("author_type")
        client = author == "client" or not author
        operator_mention = (author in ("staff", "blake")
                            and att.get("surface") == "mention"
                            and att.get("identity_reason") == "operator list")
        if client or operator_mention:
            requester.append([m.get("id"), m.get("created_at"), m.get("body")])
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    requester.sort(key=encode)
    payload = requester or [ticket.get("id"), ticket.get("created_at"),
                            ticket.get("raw_text")]
    return hashlib.sha256(encode(payload).encode("utf-8")).hexdigest()


_FIXER_REQUEST_IDENTITY_FIELDS = (
    "product", "client_id", "bot_identity", "slack_user_id",
    "slack_channel_id", "slack_thread_ts",
)


def _fresh_fixer_request(bus, ticket, att, *, body="", require_direct_answer=False):
    """Return the fresh ticket only when its requester identity matches the row.

    This is intentionally independent of deployment evidence. Grounded FIXER
    answers have no PR to prove, but they still must answer the request that is
    current at the moment of delivery and resolution. The monotonic database
    version closes the hash/read-to-resolve race; the hash still binds the exact
    requester transcript. Tenant, bot, user and destination may not drift between
    any two fresh reads. Missing or unreadable durable identity fails closed.
    """
    stamped = (att or {}).get("request_key")
    version = (att or {}).get("request_version")
    if (not isinstance(stamped, str) or not stamped
            or not isinstance(version, int) or isinstance(version, bool)
            or version < 0 or not isinstance(ticket, dict)):
        return None
    try:
        fresh = bus.ticket(ticket["id"])
        current = _current_fixer_request_key(bus, fresh) if fresh else None
    except Exception:  # noqa: BLE001 - unreadable identity is never current proof
        return None
    if (not isinstance(fresh, dict)
            or fresh.get("request_version") != version
            or any(fresh.get(field) != ticket.get(field)
                   for field in _FIXER_REQUEST_IDENTITY_FIELDS)
            or not current or stamped != current):
        return None
    if require_direct_answer and not _direct_answerable_question(fresh, body):
        return None
    return fresh


def _channel_for(kind, identity):
    if kind == _a.KIND_FIXER_REQUEST:
        return config.ops_fix_channel_id()
    return identity.fixer_channel() or config.fixer_channel_id()


def _person_for_card(bus, ticket, identity):
    """m2: a post-time hold card names the person and gym in words, like a draft-time one.

    The outbox has no identity_gate result to hand (it runs long after resolution), so this
    reconstructs the same line from the ticket row itself -- reporter, gym, identity kind --
    rather than falling back to a bare Slack id, which is exactly the unreadable card D53
    was written to get rid of."""
    t = ticket or {}
    uid = str(t.get("slack_user_id") or "") or "?"
    kind = str(t.get("identity_kind") or "unknown")
    email = str(t.get("reporter") or "").strip()
    gym = str(t.get("client_id") or "").strip()
    label = ""
    try:
        if gym:
            rows = bus._get("gyms", {"id": f"eq.{gym}", "select": "name", "limit": "1"})
            label = str((rows or [{}])[0].get("name") or "").strip()
    except Exception:  # noqa: BLE001 - a name lookup never blocks a card
        label = ""
    bits = [f"{kind} {_a._slack_escape(uid)}"]
    if email:
        bits.append(_a._slack_escape(email))
    bits.append(f"gym {_a._slack_escape(label or gym or 'not resolved')}")
    return ", ".join(bits)


def _client_dm_lane_meta_key():
    """The attachments key client_dm_support stamps on every row it writes
    (lane.py's LANE_META), imported by name rather than duplicated as a magic
    string here so the two can never drift apart."""
    from ..client_dm_support.lane import LANE_META
    return LANE_META


def _recipient_armed(identity, recipient_kind):
    if recipient_kind in ("staff", "coach"):
        return config.slack_convo_staff_reply_armed(identity.name)
    return config.slack_convo_client_reply_armed(identity.name)


def _parse_ts(value):
    try:
        s = str(value or "").replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _age_seconds(row, now=None):
    att = row.get("attachments") or {}
    born = (_parse_ts(att.get("claimed_at")) or _parse_ts(att.get("released_at"))
           or _parse_ts(row.get("created_at")))
    if born is None:
        return 0
    return ((now or datetime.now(timezone.utc)) - born).total_seconds()


def hold_notice_blocks(row):
    """Block Kit for a hold notice: the FULL text across as many sections as it needs (RA-M2
    -- what Blake reviews before tapping must be everything that will post, never a
    truncated prefix), plus ONE button whose value is the held row id. listener_wiring
    routes that action id to release_held, operator-gated."""
    body = row.get("body") or ""
    att = row.get("attachments") or {}
    mid = att.get("held_message_id") or ""
    chunks = [body[i:i + _BLOCK_TEXT_CHARS] for i in range(0, len(body), _BLOCK_TEXT_CHARS)] or [""]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks]
    if mid:
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "action_id": RELEASE_ACTION_ID, "value": str(mid),
            "text": {"type": "plain_text", "text": "Release"}, "style": "primary"}]})
    return blocks


def escalation_blocks(row, ticket):
    """Block Kit for an escalation card: the full text, plus ONE button that closes the loop
    back to the person who wrote in (D48).

    An escalation used to be the end of the line for the submitter. The card reached #fixer,
    Blake dealt with it, and nothing ever went back -- no acknowledgement, no "this is
    handled". The button is the missing half: listener_wiring routes it (operator-gated) to
    resolve_and_notify below, which writes the person a status row and closes the ticket.

    Rendered only when there IS somewhere to send that notice (a portal thread or an already
    opened group DM) and the ticket is not already closed; a button that could only no-op is
    worse than no button."""
    body = row.get("body") or ""
    tid = str((ticket or {}).get("id") or "")
    chunks = [body[i:i + _BLOCK_TEXT_CHARS] for i in range(0, len(body), _BLOCK_TEXT_CHARS)] or [""]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks]
    reachable = portal_deliverable(ticket) or bool((ticket or {}).get("slack_channel_id"))
    if tid and reachable and (ticket or {}).get("status") != "resolved":
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "action_id": RESOLVE_ACTION_ID, "value": tid,
            "text": {"type": "plain_text", "text": "Resolved, tell them"}}]})
    return blocks


CLAIM_TIMEOUT_SECONDS = 90


def _claim(bus, row, log):
    """Ready/held -> posting, a conditional PATCH only one caller can win (N4). Also stamps
    claimed_at (a second, non-atomic write, but safe: we already exclusively own the row --
    the CAS matched only for us) so _recover_stale_claims can tell a row that just started
    posting from one truly orphaned by a crash (D26)."""
    try:
        if not bus.claim_message(row["id"]):
            return False
    except AttributeError:
        return True  # a bus without claim support (older fakes): best effort, no CAS
    except Exception as e:  # noqa: BLE001
        log(f"[slack-convo/outbox] claim failed for row {row['id']}: {type(e).__name__}")
        return False
    try:
        bus.mark_message(row["id"], "posting",
                         meta_update={"claimed_at": datetime.now(timezone.utc).isoformat()})
    except Exception:  # noqa: BLE001 - best-effort stamp; the claim itself already succeeded
        pass
    return True


def _recover_stale_claims(bus, identity, log, now=None):
    """A row still 'posting' after CLAIM_TIMEOUT_SECONDS is orphaned -- either a crash
    between claim and mark in THIS process, or (D26, the scenario the claim step exists for)
    a redeploy overlap / second consumer per D2 that crashed mid-post. Swept back to 'ready'
    so it is retried rather than stuck forever.

    D26 (2026-09-03, MAJOR): this used to sweep EVERY 'posting' row unconditionally, with no
    staleness check. Under exactly the multi-consumer scenario it exists to protect against,
    a row genuinely mid-flight in process A (a slow Slack API call) would be un-claimed by
    process B's very next 5-second sweep and re-posted while A was still in flight -- a
    duplicate post, the opposite of the guarantee. The timeout is generous over realistic
    post() latency so a live post is never stolen out from under it."""
    try:
        stuck = bus.outbox("posting", limit=200, identity=identity.name)
    except TypeError:
        try:
            stuck = bus.outbox("posting", limit=200)
        except Exception:  # noqa: BLE001
            return 0
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for row in stuck:
        if _age_seconds(row, now) < CLAIM_TIMEOUT_SECONDS:
            continue  # plausibly still in flight; do not steal it
        try:
            bus.mark_message(row["id"], "ready", meta_update={"reclaimed_stale_posting": True})
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


def _blake_is_member(identity, channel, user):
    """Read every Slack membership page; an incomplete or unsupported read is not proof."""
    if not channel.startswith(("C", "G")) or not user:
        return False
    from slack_sdk import WebClient
    client = WebClient(token=identity.env(identity.bot_token_env), timeout=5)
    cursor = None
    for _ in range(100):
        args = {"channel": channel, "limit": 200}
        if cursor:
            args["cursor"] = cursor
        response = client.conversations_members(**args)
        if not response.get("ok"):
            return False
        if user in (response.get("members") or []):
            return True
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return False
    return False


def run_once(bus, post, *, identity, log=print, limit=50, now=None,
             member_check=None):
    """Process up to `limit` ready rows for THIS identity.
    post(channel, text, thread_ts=None, blocks=None) -> slack ts.
    Returns a summary dict. Never raises out of the loop."""
    summary = {"posted": 0, "held": 0, "suppressed": 0, "failed": 0, "skipped": 0,
               "resolved": 0, "reclaimed": 0}
    summary["reclaimed"] = _recover_stale_claims(bus, identity, log, now=now)
    try:
        rows = bus.outbox("ready", limit=limit, identity=identity.name)
    except TypeError:  # a bus without the identity filter (older fakes)
        rows = bus.outbox("ready", limit=limit)
    except Exception as e:  # noqa: BLE001
        log(f"[slack-convo/outbox] read failed: {type(e).__name__}")
        return summary
    for row in rows:
        try:
            _dispatch_one(bus, post, row, identity=identity, log=log, summary=summary,
                          now=now, member_check=member_check or
                          (lambda channel, user: _blake_is_member(identity, channel, user)))
        except Exception as e:  # noqa: BLE001 - one row never stalls the queue
            log(f"[slack-convo/outbox] row {row.get('id')} failed: {type(e).__name__}")
            try:
                bus.mark_message(row["id"], "failed")
            except Exception:  # noqa: BLE001
                pass
            summary["failed"] += 1
    return summary


def _suppress(bus, row, ticket, identity, why, log, summary, *, escalate=True):
    log(f"[slack-convo/outbox] SUPPRESSED row {row['id']}: {why}")
    bus.mark_message(row["id"], "suppressed", meta_update={"suppressed_why": why})
    summary["suppressed"] += 1
    if escalate and ticket:
        # V-M5: a human sees every reply the bot declined to send.
        bus.record_outbound(
            ticket_id=ticket["id"], author_type="system",
            body=(f"SUPPRESSED reply on ticket {ticket['id']} ({identity.name}): {why}. "
                  f"Nothing was posted; a person should look."),
            delivery_status="ready", kind=_a.KIND_ESCALATION,
            meta={"identity": identity.name, "suppressed_message_id": row["id"],
                  "recipient_kind": (row.get("attachments") or {}).get("recipient_kind")})


def _dispatch_one(bus, post, row, *, identity, log, summary, now=None,
                  member_check=None):
    att = row.get("attachments") or {}
    kind = att.get("kind") or ""
    ticket = bus.ticket(row["ticket_id"])
    if not ticket:
        _suppress(bus, row, None, identity, "parent ticket missing", log, summary,
                  escalate=False)
        return
    # only rows for THIS identity; another identity's loop owns the rest
    row_ident = att.get("identity") or ""
    if row_ident and row_ident != identity.name:
        summary["skipped"] += 1
        return
    # Scout raises this system alert precisely when the portal bridge cannot
    # establish the ticket's bot identity. Portal tickets belong to Scout's
    # outbox, while Echo tickets belong to Echo's. Route only this stamped
    # internal escalation; never grant a conversational row this bypass.
    portal_provenance_alert = (
        kind == _a.KIND_ESCALATION
        and row.get("direction") == "outbound"
        and row.get("author_type") == "system"
        and att.get("surface") == "portal_bridge_provenance"
        and row_ident == identity.name
        and (ticket.get("product"), identity.name) in {
            ("echo", "echo"), ("portal", "scout")})
    if (ticket.get("bot_identity") or "") != identity.name and not portal_provenance_alert:
        summary["skipped"] += 1
        return
    # 0. fail closed on anything we do not recognise
    if kind not in _a.ALL_KINDS:
        _suppress(bus, row, ticket, identity, f"unknown kind {kind!r}", log, summary)
        return
    if not row_ident:
        _suppress(bus, row, ticket, identity, "row carries no identity stamp", log, summary)
        return

    # ---- internal kinds: fixer / ops-fix channels, never the person's thread ---------
    if kind in _a.INTERNAL_KINDS:
        channel = _channel_for(kind, identity)
        if not channel:
            log(f"[slack-convo/outbox] no channel configured for {kind}; row "
                f"{row['id']} marked failed (set AGENT_FIXER_CHANNEL_ID / the ops-fix "
                "channel)")
            bus.mark_message(row["id"], "failed")
            summary["failed"] += 1
            return
        if not _claim(bus, row, log):
            summary["skipped"] += 1
            return
        if kind == _a.KIND_HOLD_NOTICE:
            blocks = hold_notice_blocks(row)
        elif kind == _a.KIND_ESCALATION and not portal_provenance_alert:
            blocks = escalation_blocks(row, ticket)
        else:
            blocks = None
        try:
            ts = post(channel, row["body"], thread_ts=None, blocks=blocks)
        except Exception as e:  # noqa: BLE001
            # Audit 4, finding 2: a hold card or escalation that fails to post is the case
            # where a human never learns anything -- while the client may already have been
            # acknowledged. It must be LOUD; silence here is the whole failure mode.
            log(f"[slack-convo/outbox] CRITICAL internal {kind} FAILED to reach "
                f"{channel} for ticket {ticket['id']}: {type(e).__name__}: {e}. "
                f"Nobody has been told about this ticket.")
            raise
        bus.mark_message(row["id"], "posted", slack_ts=ts)
        summary["posted"] += 1
        return

    # ---- conversational kinds: the gates ---------------------------------------------
    # Re-read deployment proof at dispatch time. A queued FIXER acknowledgement,
    # held-answer replacement, or stale notice must never reach a client.
    customer_fix = _customer_fix_reply(ticket, att, row.get("body") or "")
    fixer_grounded_answer = _fixer_grounded_question_answer(
        ticket, att, kind, row.get("body") or "")
    if customer_fix:
        if not _verified_fix_notice(ticket, att, kind, bus=bus, now=now):
            _suppress(bus, row, ticket, identity,
                      "customer fix reply requires current PR merged, deployed and verified",
                      log, summary)
            return
        release_key = ((ticket.get("verification_after") or {}).get("fixer") or {}).get("request_key")
        try:
            current_key = _current_fixer_request_key(bus, ticket)
        except Exception as e:  # noqa: BLE001 - unreadable thread must not close a ticket
            log(f"[slack-convo/outbox] request read failed for {ticket['id']}: "
                f"{type(e).__name__}")
            current_key = None
        fresh_request = _fresh_fixer_request(bus, ticket, att)
        if (not fresh_request or not current_key or release_key != current_key
                or att.get("request_key") != current_key):
            _suppress(bus, row, ticket, identity,
                      "customer fix reply does not match the current requester messages",
                      log, summary)
            return
        ticket = fresh_request
    elif fixer_grounded_answer and not _fresh_fixer_request(
            bus, ticket, att, body=row.get("body") or "", require_direct_answer=True):
        _suppress(bus, row, ticket, identity,
                  "grounded FIXER answer does not match the current requester messages",
                  log, summary)
        return
    # 1. first contact
    if bus.inbound_count(ticket["id"]) < 1:
        _suppress(bus, row, ticket, identity,
                  "ticket has no inbound human message; the bot never speaks first",
                  log, summary)
        return
    # 2. verification for anything substantive
    if kind == _a.KIND_ANSWER and not ticket.get("verification_after"):
        _suppress(bus, row, ticket, identity, "answer with no verification_after", log,
                  summary)
        return
    # 3. no re-entry into the ops-fix worker through our own mouth
    if _REENTRY_PREFIX in (row.get("body") or "").upper():
        _suppress(bus, row, ticket, identity, "reply body carries the ops-fix trigger prefix",
                  log, summary)
        return
    # 4. freshness
    if _age_seconds(row, now) > STALE_AFTER_SECONDS:
        _suppress(bus, row, ticket, identity,
                  f"row sat in ready for over {STALE_AFTER_SECONDS // 3600}h", log, summary)
        return
    # 5. trust ladder, re-checked at post time; held rows always get a card (V-M8). A row
    # Blake has explicitly released is the one exception: his tap already IS the approval.
    recipient_kind = att.get("recipient_kind") or ticket.get("identity_kind") or "client"
    # 5a. D54 hard lines, re-checked at POST time (a flag can flip, and a row can be written
    # by an older process). A substantive ANSWER to a client posts on its own ONLY with that
    # identity's AUTO_ANSWER flag armed, and NEVER when the draft-time check marked it
    # forbidden. Blake's release tap is still the way any of these actually go out.
    if kind == _a.KIND_ANSWER and recipient_kind not in ("staff", "coach") \
            and not att.get("released_by"):
        # M2 (audit 2): this used to trust the stored marker alone, so a row written by any
        # writer that omits it -- a pre-D54 process, a future one -- posted a hard-line
        # answer to a client with no tap. The body is re-read here, which is what "checked
        # again at post time" has to mean for a rule that is about content.
        # Audit 4, finding 4: this re-read only the DENYLIST against the body, so the claim
        # "both are checked at draft time AND at post time" was false and the whole point of
        # the single may_auto_answer decision (that no path enforces half the rule) did not
        # hold for the post-time path. The ticket carries the question -- raw_text -- so the
        # identical decision can be, and now is, made here too.
        # D72 (2026-09-11): the verdict names the rule and carries a TIER. A row the FIXER
        # authored (attachments.fixer) is checked against the org floor only; an Echo
        # draft gets the structural checks too. Either way a hold is never silent:
        # hold_answer_for_team writes the team card and escalates the ticket.
        # Portal holds wait for verification and Blake's customer handoff.
        fixer_authored = bool(att.get("fixer"))
        verdict = _a.auto_answer_verdict(ticket.get("raw_text") or "", row.get("body"),
                                         grounded_by_fixer=fixer_authored)
        if att.get("auto_answer_forbidden") and verdict.ok:
            # A draft-time check marked this org floor; the marker is a hard signal even
            # when the body re-read passes (an older, stricter rule may have written it).
            verdict = _a.AnswerVerdict(False, _a.HOLD_TIER_ORG_FLOOR,
                                       "draft_time_forbidden_marker")
        if verdict.held:
            bus.mark_message(row["id"], "held",
                             meta_update={"held_why": f"{verdict.tier}: {verdict.rule}",
                                          "hold_tier": verdict.tier,
                                          "hold_rule": verdict.rule})
            summary["held"] += 1
            _a.hold_answer_for_team(
                bus, ticket=ticket, ident_name=identity.name, recipient_kind=recipient_kind,
                user=ticket.get("slack_user_id") or "?", account_key=None,
                surface=att.get("surface") or "", body=row.get("body") or "",
                held_message_id=row["id"], verdict=verdict,
                person=_person_for_card(bus, ticket, identity),
                fixer_authored=fixer_authored,
                client_notice=(att.get("surface") != "portal_ticket_bridge"
                               and not portal_deliverable(ticket)), log=log)
            return
        if not config.slack_convo_auto_answer_armed(identity.name):
            flag = f"SLACK_CONVO_{identity.name.upper()}_AUTO_ANSWER"
            bus.mark_message(row["id"], "held",
                             meta_update={"held_why": "auto answer not armed",
                                          "hold_tier": _a.HOLD_TIER_UNARMED})
            summary["held"] += 1
            _a.hold_answer_for_team(
                bus, ticket=ticket, ident_name=identity.name, recipient_kind=recipient_kind,
                user=ticket.get("slack_user_id") or "?", account_key=None,
                surface=att.get("surface") or "", body=row.get("body") or "",
                held_message_id=row["id"],
                verdict=_a.AnswerVerdict(False, _a.HOLD_TIER_UNARMED, "auto_answer_not_armed"),
                person=_person_for_card(bus, ticket, identity),
                fixer_authored=fixer_authored, unarmed_flag=flag,
                client_notice=(att.get("surface") != "portal_ticket_bridge"
                               and not portal_deliverable(ticket)), log=log)
            return
    # 5b. GAP 2 (audit of PR #68): a row THIS SPECIFIC LANE wrote must re-verify that
    # lane's OWN full three-flag interlock at dispatch time, not only the general
    # _recipient_armed check above. arming.preflight() checks AGENT_CLIENT_DM_AUTOFIX,
    # AGENT_CLIENT_DM_CLIENT_REPLY and a runtime-derived AGENT_CLIENT_DM_LIVE_ACK --
    # none of which _recipient_armed (SLACK_CONVO_<ID>_CLIENT_REPLY alone) has ever
    # read. Without this, a row written while the lane was briefly LIVE stays in
    # 'ready' after AGENT_CLIENT_DM_AUTOFIX is later revoked (or the ack goes stale),
    # and this same _recipient_armed check -- true the whole time, since it is a
    # different, pre-existing, already-armed flag -- would still release it with zero
    # awareness this lane, or its revocation, exists. Scoped to rows carrying this
    # lane's own provenance marker, so no other identity's or lane's delivery changes.
    if not att.get("released_by") and att.get(_client_dm_lane_meta_key()):
        from ..client_dm_support import arming as _cdm_arm
        lane_arm = _cdm_arm.preflight(identity.name)
        if lane_arm.mode != _cdm_arm.MODE_LIVE:
            bus.mark_message(row["id"], "held",
                             meta_update={"held_why": "client_dm_support lane no longer "
                                                       f"live at dispatch time: "
                                                       f"{lane_arm.reason}"})
            summary["held"] += 1
            _a.write_hold_notice(
                bus, ident_name=identity.name, tid=ticket["id"],
                recipient_kind=recipient_kind, user=ticket.get("slack_user_id") or "?",
                account_key=None, kind=kind, body=row.get("body") or "",
                held_message_id=row["id"], surface=att.get("surface") or "",
                person=_person_for_card(bus, ticket, identity),
                why=f"client_dm_support's own arming no longer holds at dispatch "
                    f"time (re-checked independently of SLACK_CONVO_"
                    f"{identity.name.upper()}_CLIENT_REPLY): {lane_arm.reason}")
            return
    if not att.get("released_by") and not _recipient_armed(identity, recipient_kind):
        bus.mark_message(row["id"], "held", meta_update={"held_why": "flag off at post time"})
        summary["held"] += 1
        _a.write_hold_notice(
            bus, ident_name=identity.name, tid=ticket["id"], recipient_kind=recipient_kind,
            user=ticket.get("slack_user_id") or "?", account_key=None, kind=kind,
            body=row.get("body") or "", held_message_id=row["id"],
            surface=att.get("surface") or "", why="flag off at post time")
        return
    # FIXER customer Slack messages include Blake in the actual conversation. A receipt
    # in #fixer alone is not participation in the client's channel. Read membership at
    # dispatch; Slack read failures, one-to-one DMs and unsupported channel types hold.
    channel = ticket.get("slack_channel_id")
    # A grounded answer authored by FIXER is intentionally exempt from the
    # code-fix deployment proof above, but it is still a FIXER customer
    # outbound.  Blake's membership and visible inclusion apply to every such
    # Slack message, not only to code-fix completion notices.
    fixer_customer_slack = bool(
        channel
        and recipient_kind not in ("staff", "coach")
        and (customer_fix or att.get("fixer"))
    )
    if fixer_customer_slack:
        try:
            member = bool(channel and channel.startswith(("C", "G")) and member_check and
                          member_check(channel, config.APPROVER_SLACK_ID))
        except Exception as e:  # noqa: BLE001
            log(f"[slack-convo/outbox] membership read failed for {channel}: "
                f"{type(e).__name__}")
            member = False
        if not member:
            _suppress(bus, row, ticket, identity,
                      "FIXER customer Slack reply requires verified Blake membership "
                      "in destination conversation", log, summary)
            return
    # 6. claim, immediately before posting
    if not _claim(bus, row, log):
        summary["skipped"] += 1
        return
    # Membership reads and the claim itself are externally visible boundaries. A
    # correction arriving during either one invalidates a grounded FIXER answer
    # even though that answer legitimately has no deployment record.
    if fixer_grounded_answer or customer_fix:
        fresh = _fresh_fixer_request(
            bus, ticket, att, body=row.get("body") or "",
            require_direct_answer=fixer_grounded_answer)
        release_key = (((fresh or {}).get("verification_after") or {}).get("fixer") or {}).get(
            "request_key")
        if (not fresh
                or (customer_fix and (not _verified_fix_notice(
                    fresh, att, kind, bus=bus, now=now)
                    or release_key != att.get("request_key")))):
            _suppress(bus, row, ticket, identity,
                      "FIXER requester identity changed before delivery", log, summary)
            return
        ticket = fresh
    # 7. destination
    channel = ticket.get("slack_channel_id")
    surface = att.get("surface") or ""
    # F1 (audit 8, MAJOR): the audit-7 fix read the surface off the ticket's own inbound row
    # instead of its source -- and a portal ticket's inbound row carries
    # 'portal_ticket_bridge', which is not in this tuple, so the notice STILL posted as a
    # thread reply inside the group DM. Byte-identical behaviour to the bug it replaced, and
    # its test asserted the helper on a Slack MPIM ticket, never the portal case it was for.
    # People do not thread in a DM whatever brought the ticket there.
    thread_ts = (None if surface in TOP_LEVEL_SURFACES
                 else ticket.get("slack_thread_ts"))
    if not channel:
        # D48: no Slack thread is not automatically a dead end. A portal-submitted ticket
        # is delivered to the thread the person wrote it in; only a ticket with neither
        # surface fails. Before this, EVERY conversational row on a portal ticket that had
        # not been group-DMed died here -- marked failed, silently, with the person who
        # submitted the form never told anything at all (found live 2026-09-05 on three
        # escalated portal tickets).
        if not portal_deliverable(ticket):
            bus.mark_message(row["id"], "failed")
            summary["failed"] += 1
            return
        bus.mark_message(row["id"], "posted", meta_update={"delivered_via": "portal_thread"})
        summary["posted"] += 1
        _after_answer_posted(bus, ticket, row, kind, summary, att, identity, log)
        # m4: no Slack call happens on this branch -- "posted" here means migration 0310 now
        # lets the client read it in the thread they wrote from. The receipt says exactly
        # that rather than claiming a message was pushed to them.
        _receipt(bus, ticket, row, identity, kind, att,
                 where="released into the portal support thread they wrote from",
                 summary=summary)
        return
    sent_body = row["body"]
    if fixer_customer_slack:
        mention = f"<@{config.APPROVER_SLACK_ID}>"
        if mention not in sent_body:
            sent_body = f"{mention} {sent_body}"
        # Persist the exact body BEFORE posting, so the portal thread and subsequent
        # receipt cannot disagree with what Slack actually received.
        if sent_body != row["body"]:
            stored = bus.set_message_body_if_posting(row["id"], sent_body)
            if not stored or stored.get("body") != sent_body:
                raise RuntimeError("FIXER Slack body update was not confirmed")
            row = {**row, "body": sent_body}
    # Persisting Blake's exact mention/body is another mutation window. Re-read
    # the durable requester identity at the last possible point before Slack.
    if fixer_grounded_answer or customer_fix:
        fresh = _fresh_fixer_request(
            bus, ticket, att, body=row.get("body") or "",
            require_direct_answer=fixer_grounded_answer)
        release_key = (((fresh or {}).get("verification_after") or {}).get("fixer") or {}).get(
            "request_key")
        if (not fresh
                or fresh.get("slack_channel_id") != channel
                or fresh.get("slack_thread_ts") != ticket.get("slack_thread_ts")
                or (customer_fix and (not _verified_fix_notice(
                    fresh, att, kind, bus=bus, now=now)
                    or release_key != att.get("request_key")))):
            _suppress(bus, row, fresh or ticket, identity,
                      "FIXER requester identity changed before Slack delivery", log, summary)
            return
        ticket = fresh
    ts = post(channel, sent_body, thread_ts=thread_ts, blocks=None)
    bus.mark_message(row["id"], "posted", slack_ts=ts)
    summary["posted"] += 1
    _after_answer_posted(bus, ticket, row, kind, summary, att, identity, log)
    _receipt(bus, ticket, row, identity, kind, att, where=f"Slack {channel}", summary=summary)


# Kinds worth a receipt in #fixer. An `ack` ("checking that for you now") is noise; what
# Blake asked to see is anything SUBSTANTIVE that reached the client without him: the answer
# itself, the honest no-draft template, and the resolution notice that closes a ticket.
RECEIPT_KINDS = frozenset({_a.KIND_ANSWER, _a.KIND_TEMPLATE, _a.KIND_STATUS})


def write_receipt(bus, ticket, *, identity, body, kind, where, auto, extra=None):
    """The shared receipt writer, so every path that tells a client something -- this
    module's outbox AND the portal bridge's direct-outreach path (M1) -- produces the same
    card. Written only AFTER a delivery actually succeeded."""
    from datetime import datetime as _dt, timezone as _tz
    sent_at = _dt.now(_tz.utc).isoformat()
    how = "SENT AUTOMATICALLY (no tap)" if auto else "sent"
    meta = {"identity": getattr(identity, "name", ""), "receipt": True,
            "receipt_kind": kind, "auto_answer": bool(auto), "sent_at": sent_at}
    if extra:
        meta.update(extra)
    return bus.record_outbound(
        ticket_id=ticket["id"], author_type="system",
        body=(f"RECEIPT: the client was told this, {how}.\n"
              f"BOT: {getattr(identity, 'name', '?')}   TICKET: {ticket['id']}   "
              f"KIND: {kind}\nWHERE: {where}   WHEN: {sent_at}\n"
              f"STATUS NOW: {(bus.ticket(ticket['id']) or ticket).get('status') or '?'}\n\n"
              f"{body or ''}"),
        delivery_status="ready", kind=_a.KIND_ESCALATION, meta=meta)


def _receipt(bus, ticket, row, identity, kind, att, *, where, summary):
    """D55: a receipt of what the client was ACTUALLY told, posted to #fixer.

    Blake, 2026-09-05: "so Blake never has to wonder whether a ticket actually landed with
    the client". This is written only AFTER the row is marked posted, and it quotes the real
    body that went out with the real timestamp, so it can never claim a delivery that did not
    happen. It is an internal kind: it goes to the fixer channel and never into the person's
    thread. A failure here never fails the post that already succeeded."""
    if kind not in RECEIPT_KINDS:
        return
    if (att or {}).get("recipient_kind") in ("staff", "coach"):
        return  # staff can see their own thread; a receipt would just be an echo
    try:
        sent_at = datetime.now(timezone.utc).isoformat()
        # m4: re-read the ticket so STATUS NOW is the status now, not the one captured
        # before _resolve_on_answer ran a line earlier.
        fresh = None
        try:
            fresh = bus.ticket(ticket["id"])
        except Exception:  # noqa: BLE001
            fresh = None
        status_now = (fresh or ticket).get("status") or "?"
        auto = bool(kind == _a.KIND_ANSWER and not (att or {}).get("released_by"))
        how = ("SENT AUTOMATICALLY (no tap)" if auto else
               ("sent after your tap" if (att or {}).get("released_by") else "sent"))
        # FIXER provenance can remain after a human tap; only the release actor counts.
        if (att or {}).get("released_by") == "fixer":
            how = "sent automatically by FIXER"
        owner = f"<@{config.APPROVER_SLACK_ID}> " if (att or {}).get("fixer") else ""
        bus.record_outbound(
            ticket_id=ticket["id"], author_type="system",
            body=(f"{owner}RECEIPT: the client was told this, {how}.\n"
                  f"BOT: {identity.name}   TICKET: {ticket['id']}   KIND: {kind}\n"
                  f"WHERE: {where}   WHEN: {sent_at}\n"
                  f"STATUS NOW: {status_now}\n\n{row.get('body') or ''}"),
            # C3: an ESCALATION row, because that is a kind the portal already hides from
            # clients. attachments.receipt marks it as a receipt for everything on this side.
            delivery_status="ready", kind=_a.KIND_ESCALATION,
            meta={"identity": identity.name, "receipt_for": row["id"], "receipt": True,
                  "receipt_kind": kind, "auto_answer": auto, "sent_at": sent_at})
    except Exception:  # noqa: BLE001 - never undo a successful post over a receipt
        pass


def _after_answer_posted(bus, ticket, row, kind, summary, att, identity, log=print):
    """Round 2 (audit of PR #107, MAJOR 6): what happens to the ticket once an answer is
    with the person. An answer that promised a HUMAN follow-up does not close the ticket --
    it is routed to the FIXER with adapter.FOLLOW_UP_MARKER (idempotent: the Slack adapter
    may already have done this at draft time). Every other answer resolves as before."""
    if kind == _a.KIND_ANSWER and (att or {}).get("recipient_kind") not in ("staff", "coach") \
            and _a.promises_human_follow_up(row.get("body") or ""):
        _a.route_follow_up_promise(bus, ticket, ident_name=identity.name,
                                   body=row.get("body") or "",
                                   recipient_kind=(att or {}).get("recipient_kind") or "client",
                                   surface=(att or {}).get("surface") or "",
                                   person=_person_for_card(bus, ticket, identity), log=log)
        return
    _resolve_on_answer(bus, ticket, kind, summary, att, row.get("body") or "")


def _resolve_on_answer(bus, ticket, kind, summary, att=None, body=""):
    """V-M4: the ticket closes when the person HAS the message, not when we drafted it.

    Two rows close a ticket: the ANSWER that answered it, and the resolve NOTICE a human
    tapped (MINOR 5, audit 7 -- resolve_and_notify used to stamp the ticket itself, before
    delivery, so a failed post left a ticket claiming to be resolved over a failed row)."""
    meta = att or {}
    fixer = bool(meta.get("fixer"))
    should_resolve = (
        kind == _a.KIND_ANSWER and ticket.get("status") == "verification"
        or kind == _a.KIND_STATUS and meta.get("resolve_notice") is True
        and ticket.get("status") != "resolved")
    if fixer and should_resolve:
        # Slack (or the portal thread) may accept a delivery while the requester
        # corrects it or an operator changes its eligibility. Keep the receipt for
        # exactly what was delivered, but resolve only through the database CAS
        # over the complete ticket identity that was freshly validated here.
        grounded = _fixer_grounded_question_answer(ticket, meta, kind, body)
        fresh = _fresh_fixer_request(
            bus, ticket, meta, body=body, require_direct_answer=grounded)
        resolver = getattr(bus, "resolve_current_delivery", None)
        if not fresh or not callable(resolver):
            return
        if meta.get("resolve_notice"):
            release_key = ((fresh.get("verification_after") or {}).get("fixer") or {}).get(
                "request_key")
            if release_key != meta.get("request_key"):
                return
        expected = {
            "status": fresh.get("status"),
            "classification": fresh.get("classification"),
            **{field: fresh.get(field) for field in _FIXER_REQUEST_IDENTITY_FIELDS},
        }
        try:
            resolved = resolver(
                ticket["id"], meta.get("request_version"),
                expected["status"], expected["classification"],
                expected["product"], expected["client_id"],
                expected["bot_identity"], expected["slack_user_id"],
                expected["slack_channel_id"], expected["slack_thread_ts"])
        except Exception:  # noqa: BLE001 - a failed atomic close leaves it open
            return
        if (not isinstance(resolved, dict)
                or resolved.get("id") != ticket.get("id")
                or resolved.get("status") != "resolved"
                or resolved.get("request_version") != meta.get("request_version")
                or resolved.get("classification") != expected["classification"]
                or resolved.get("escalated") is not False
                or resolved.get("hold_tier") is not None
                or any(resolved.get(field) != expected[field]
                       for field in _FIXER_REQUEST_IDENTITY_FIELDS)):
            return
        summary["resolved"] += 1
        return
    if fixer:
        # A FIXER row that is not presently eligible to resolve must never fall
        # through to the legacy unconditional ticket PATCH below.
        return
    if kind == _a.KIND_ANSWER and ticket.get("status") == "verification":
        bus.set_ticket(ticket["id"], status="resolved")
        summary["resolved"] += 1
    elif kind == _a.KIND_STATUS and (att or {}).get("resolve_notice"):
        if ticket.get("status") != "resolved":
            bus.set_ticket(ticket["id"], status="resolved")
            summary["resolved"] += 1


def release_held(bus, message_id, *, approved_by, identity=None, log=print):
    """A human tap on a hold notice: flip that held row to ready and stamp the ticket.
    Returns True when a held row was released. Refuses anything not currently held, any
    kind that is not a reply or fixer request, and (when `identity` is given) any row
    another bot wrote (V-m10). The release is stamped (N2: read by gate 5 above to skip the
    trust-ladder recheck exactly once for this row) and restarts the freshness clock."""
    row = bus.message(message_id)
    if not row or row.get("delivery_status") != "held":
        return False
    att = row.get("attachments") or {}
    kind = att.get("kind") or ""
    if kind not in (_a.CONVERSATIONAL_KINDS | {_a.KIND_FIXER_REQUEST}):
        log(f"[slack-convo/outbox] release refused: row {message_id} kind {kind!r}")
        return False
    if identity is not None and (att.get("identity") or "") != identity.name:
        log(f"[slack-convo/outbox] release refused: row {message_id} belongs to "
            f"{att.get('identity') or '?'} not {identity.name}")
        return False
    bus.mark_message(message_id, "ready",
                     meta_update={"released_at": datetime.now(timezone.utc).isoformat(),
                                  "released_by": approved_by})
    try:
        bus.set_ticket(row["ticket_id"], approved_by=approved_by, approved_via="slack_button",
                       approved_at=datetime.now(timezone.utc).isoformat())
    except Exception as e:  # noqa: BLE001 - the release itself already happened
        log(f"[slack-convo/outbox] approval stamp failed: {type(e).__name__}")
    return True


def resolve_and_notify(bus, ticket_id, *, approved_by, identity, log=print):
    """A human tap on an escalation card: tell the person it is handled, and close the
    ticket (D48).

    The notice is written as a normal conversational row, so it goes out through every gate
    this module already enforces (first contact, trust ladder, freshness, claim) and lands
    wherever that ticket's person actually is: the group DM if one was opened, the portal
    support thread otherwise. Nothing is posted from here directly.

    Refuses, returning False, when: the ticket is gone, it belongs to another bot (the same
    cross-identity rule release_held holds), it is already resolved (the tap is idempotent --
    a second press must not write a second notice), or there is nowhere to deliver."""
    ticket = bus.ticket(ticket_id)
    if not ticket:
        log(f"[slack-convo/outbox] resolve refused: no ticket {ticket_id}")
        return False
    if identity is not None and (ticket.get("bot_identity") or "") != identity.name:
        log(f"[slack-convo/outbox] resolve refused: ticket {ticket_id} belongs to "
            f"{ticket.get('bot_identity') or '?'} not {identity.name}")
        return False
    if ticket.get("status") == "resolved":
        return False
    customer_fix = _customer_fix_reply(ticket, {})
    if customer_fix:
        def refuse_fix(reason):
            why = f"Resolve tap on ticket {ticket_id} did NOT go through: {reason}. " \
                  "The ticket is unchanged."
            log(f"[slack-convo/outbox] {why}")
            try:
                bus.record_outbound(
                    ticket_id=ticket_id, author_type="system", body=why,
                    delivery_status="ready", kind=_a.KIND_ESCALATION,
                    meta={"identity": getattr(identity, "name", ""),
                          "resolve_refused": True})
            except Exception:  # noqa: BLE001 - refusal still stands
                pass
            return False

    # MINOR 5's fix moved the resolved stamp to delivery time, which quietly broke what the
    # status check had been doing double duty for: idempotence. A second tap before the
    # notice posts would have written a SECOND notice. The notice row itself is the record of
    # "this tap already happened", so that is what is checked.
    if _resolve_notice_exists(bus, ticket_id):
        return False
    if not portal_deliverable(ticket) and not ticket.get("slack_channel_id"):
        log(f"[slack-convo/outbox] resolve refused: ticket {ticket_id} has no delivery "
            "surface (no portal thread, no group DM)")
        return False
    # Audit 4, finding 9: this marked the ticket resolved and returned True even when the
    # notice would be HELD by the trust ladder -- Blake taps "Resolved, tell them", the tap
    # reports ok, the ticket reads resolved, and the client is never told. If we cannot
    # deliver the notice, we do not claim the resolution.
    recipient_kind = ticket.get("identity_kind") or "client"
    if not _recipient_armed(identity, recipient_kind):
        # Audit 5, finding 4: refusing silently is its own version of the dead button this
        # whole path exists to fix -- Blake taps "Resolved, tell them" and gets nothing at
        # all. The refusal is written back to the fixer channel, naming the flag to flip.
        flag = ("STAFF_REPLY" if recipient_kind in ("staff", "coach") else "CLIENT_REPLY")
        why = (f"Resolve tap on ticket {ticket_id} did NOT go through: the notice to the "
               f"{recipient_kind} would be held because SLACK_CONVO_"
               f"{getattr(identity, 'name', '?').upper()}_{flag} is off, and marking a "
               f"ticket resolved that the person was never told about is the lie this "
               f"button exists to prevent. Arm that flag, or reply to them directly and "
               f"close it by hand. The ticket is unchanged.")
        log(f"[slack-convo/outbox] {why}")
        try:
            bus.record_outbound(
                ticket_id=ticket_id, author_type="system", body=why,
                delivery_status="ready", kind=_a.KIND_ESCALATION,
                meta={"identity": getattr(identity, "name", ""), "resolve_refused": True})
        except Exception:  # noqa: BLE001 - the refusal itself already stands
            pass
        return False
    if customer_fix:
        proof_meta = {"resolve_notice": True, "pr_url": ticket.get("fix_pr_url")}
        if not _verified_fix_notice(ticket, proof_meta, _a.KIND_STATUS, bus=bus):
            return refuse_fix("customer fix has no current merged, deployed and "
                              "independently verified business postcondition")
        try:
            current_key = _current_fixer_request_key(bus, ticket)
        except Exception:  # noqa: BLE001 - a human tap cannot waive unreadable context
            current_key = None
        release_key = ((ticket.get("verification_after") or {}).get("fixer") or {}).get("request_key")
        if not current_key or release_key != current_key:
            return refuse_fix("customer request changed or could not be verified")
        request_version = ticket.get("request_version")
        if (not isinstance(request_version, int) or isinstance(request_version, bool)
                or request_version < 0):
            return refuse_fix("customer request version is unavailable")
        if not str(ticket.get("slack_channel_id") or "").startswith(("C", "G")):
            return refuse_fix("customer fix has no group conversation for Blake to join")
    # MINOR 4 (audit 7): `surface` was the ticket's SOURCE ("website_tab"), which is not one
    # of the surfaces gate 7 knows, so the notice was posted as a THREAD REPLY inside a DM --
    # a place people do not look. The real surface is on the ticket's own inbound rows.
    surface = _surface_of(bus, ticket_id) or (ticket.get("source") or "")
    bus.record_outbound(
        ticket_id=ticket_id, author_type=getattr(identity, "name", "system"),
        body=RESOLVED_NOTICE, delivery_status="ready", kind=_a.KIND_STATUS,
        # Audit 5, finding 3: this hardcoded "client" while the gate above read the ticket's
        # own identity_kind, so a staff ticket with STAFF_REPLY on and CLIENT_REPLY off
        # passed the gate and then held the row -- the exact lie the gate was added to close.
        meta={"identity": getattr(identity, "name", ""), "recipient_kind": recipient_kind,
              "surface": surface, "resolved_by": approved_by, "resolve_notice": True,
              **({"fixer": True, "request_key": current_key,
                  "request_version": request_version,
                  "pr_url": ticket.get("fix_pr_url")} if customer_fix else {})})
    # MINOR 5 (audit 7): the ticket used to be stamped resolved HERE, before the notice had
    # been delivered -- so a post failure left a ticket permanently asserting it was resolved
    # over a row marked failed. Same rule as an answer (V-M4): the ticket closes when the
    # person HAS the message. _resolve_on_answer closes it when this row posts.
    bus.set_ticket(ticket_id, approved_by=approved_by, approved_via="slack_button",
                   approved_at=datetime.now(timezone.utc).isoformat())
    return True


def _recent(bus, ticket_id, limit=200):
    """The NEWEST rows on a ticket, newest first.

    MINOR 1 (audit 8): bus.messages orders created_at.asc, so a client-side scan of its first
    200 rows reads the OLDEST 200 -- the exact bug count_escalation_cards_since was added to
    fix, reintroduced by hand in three helpers at once. For _resolve_notice_exists the
    failure direction was a DUPLICATE resolve notice to a client."""
    try:
        return bus.recent_messages(ticket_id, limit=limit)
    except AttributeError:
        rows = bus.messages(ticket_id, limit=limit) or []
        return list(reversed(rows))


def _resolve_notice_exists(bus, ticket_id):
    """True once a resolve notice has been written for this ticket, in any delivery state.
    Fails CLOSED (True) on a read failure: a duplicate notice to a client is worse than a
    tap that reports nothing happened."""
    try:
        for m in _recent(bus, ticket_id) or []:
            att = m.get("attachments") or {}
            if m.get("direction") == "outbound" and att.get("resolve_notice"):
                return True
        return False
    except Exception:  # noqa: BLE001
        return True


def _surface_of(bus, ticket_id):
    """The surface this ticket's human actually spoke on, from its own inbound rows."""
    try:
        for m in _recent(bus, ticket_id) or []:
            if m.get("direction") == "inbound":
                s = ((m.get("attachments") or {}).get("surface") or "").strip()
                if s:
                    return s
    except Exception:  # noqa: BLE001
        pass
    return ""
