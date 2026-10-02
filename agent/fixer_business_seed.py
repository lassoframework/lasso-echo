"""Trusted FIXER business-check seeds from Echo domain events.

The grade sweep knows the expected pre-regression grade and the Echo tenant key.
Those values must reach the FIXER as structured data from this producer, never be
reconstructed from the Slack alert text.  This module builds a versioned seed,
binds it to the portal client UUID, and materializes the exact ``support_tickets``
row Scout's business-proof producer consumes after a release.

The ticket id, timestamp, request key, and source event id are deterministic.  A
retry therefore collides with the same primary key and can be confirmed rather
than creating a second autonomous fix.  No client-facing row or message is ever
written here.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date


SCHEMA_VERSION = 1
SOURCE = "echo.grade_sweep.forward_book_drop"
CONTRACT_VERSION = "echo-business-evidence-v1"
CHECK_ID = "forward_book_grade_at_least"

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_GYM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}\Z")


class SeedError(ValueError):
    """The domain event cannot be bound to one trusted FIXER ticket."""


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _source_at(source_day: str) -> str:
    try:
        parsed = date.fromisoformat(str(source_day))
    except (TypeError, ValueError) as exc:
        raise SeedError("source_day must be YYYY-MM-DD") from exc
    return f"{parsed.isoformat()}T00:00:00+00:00"


def resolve_portal_client_id(gym_key: str, *, bus=None) -> str:
    """Resolve one Echo account key to exactly one portal UUID.

    The service-role bus performs an exact lookup.  Display names, registry
    guesses, and fuzzy matching are deliberately excluded from the trust path.
    """
    if not isinstance(gym_key, str) or not _GYM.fullmatch(gym_key):
        raise SeedError("invalid gym key")
    if bus is None:
        from .slack_convo.bus import Bus
        bus = Bus()
    lookup = getattr(bus, "portal_client_id", None)
    if not callable(lookup):
        raise SeedError("bus has no portal client resolver")
    client_id = lookup(gym_key)
    if not isinstance(client_id, str) or not _UUID.fullmatch(client_id):
        raise SeedError("portal client identity unconfirmed")
    return client_id


def grade_drop_seed(*, gym_key: str, client_id: str, min_total: int,
                    observed_total: int, source_day: str) -> dict:
    """Build the immutable seed at the authoritative grade-drop producer.

    ``min_total`` is the actual stored grade from immediately before this run,
    not a generic policy threshold.  That exact value is the post-release
    expectation the independent observer must meet or exceed.
    """
    if not isinstance(gym_key, str) or not _GYM.fullmatch(gym_key):
        raise SeedError("invalid gym key")
    if not isinstance(client_id, str) or not _UUID.fullmatch(client_id):
        raise SeedError("invalid portal client UUID")
    for label, value in (("min_total", min_total), ("observed_total", observed_total)):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise SeedError(f"{label} must be an integer from 0 to 100")
    if observed_total >= min_total:
        raise SeedError("grade-drop seed requires an actual regression")
    occurred_at = _source_at(source_day)
    event_identity = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "occurred_at": occurred_at,
        "gym_key": gym_key,
        "previous_total": min_total,
        "observed_total": observed_total,
    }
    # The source-event identity belongs to the grade event itself.  The current
    # portal UUID is a binding on that event, not part of its identity; if the
    # mapping changes during a retry, the same deterministic ticket collides and
    # fails readback instead of minting a second ticket for one regression.
    event_id = hashlib.sha256(_canonical(event_identity).encode("utf-8")).hexdigest()
    return {
        **event_identity,
        "client_id": client_id,
        "source_event_id": event_id,
        "check_id": CHECK_ID,
        "params": {"min_total": min_total},
    }


def prepare_grade_drop_seed(*, gym_key: str, min_total: int, observed_total: int,
                            source_day: str, bus=None) -> dict:
    """Resolve the tenant and build one trusted grade-drop seed."""
    return grade_drop_seed(
        gym_key=gym_key,
        client_id=resolve_portal_client_id(gym_key, bus=bus),
        min_total=min_total,
        observed_total=observed_total,
        source_day=source_day,
    )


def _validate_seed(seed: dict) -> dict:
    if not isinstance(seed, dict) or set(seed) != {
            "schema_version", "source", "occurred_at", "gym_key", "client_id",
            "previous_total", "observed_total", "source_event_id", "check_id", "params"}:
        raise SeedError("invalid seed shape")
    rebuilt = grade_drop_seed(
        gym_key=seed.get("gym_key"), client_id=seed.get("client_id"),
        min_total=seed.get("previous_total"),
        observed_total=seed.get("observed_total"),
        source_day=str(seed.get("occurred_at") or "")[:10],
    )
    if seed != rebuilt or not _HASH.fullmatch(str(seed.get("source_event_id") or "")):
        raise SeedError("seed identity mismatch")
    return rebuilt


def ticket_row(seed: dict, alert_text: str) -> dict:
    """Materialize Scout's exact ticket and business-check pointer contract."""
    seed = _validate_seed(seed)
    raw_text = str(alert_text or "").strip()
    if not raw_text or len(raw_text) > 4000:
        raise SeedError("alert text must be 1..4000 characters")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", raw_text):
        raise SeedError("alert text contains control characters")
    ticket_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"lasso:fixer-business-seed:{seed['source_event_id']}"))
    created_at = seed["occurred_at"]
    identity = [ticket_id, created_at, raw_text]
    request_key = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    business_check = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "ticket_id": ticket_id,
        "client_id": seed["client_id"],
        "request_key": request_key,
        "check_id": seed["check_id"],
        "params": dict(seed["params"]),
    }
    return {
        "id": ticket_id,
        "product": "echo",
        "source": "ops_fix",
        "client_id": seed["client_id"],
        "reporter": "echo_grade_sweep",
        "raw_text": raw_text,
        "classification": "code_fix",
        "severity": "P1",
        "status": "new",
        "lane": "hold",
        "hold_tier": "routine",
        "created_at": created_at,
        "verification_before": {"fixer": {
            "business_check": business_check,
            "source_event": {
                "schema_version": SCHEMA_VERSION,
                "source": seed["source"],
                "source_event_id": seed["source_event_id"],
                "occurred_at": seed["occurred_at"],
                "gym_key": seed["gym_key"],
                "previous_total": seed["previous_total"],
                "observed_total": seed["observed_total"],
            },
        }},
    }


def persist(seed: dict, alert_text: str, *, bus=None) -> dict:
    """Write or confirm the deterministic row through the service-auth bus."""
    row = ticket_row(seed, alert_text)
    if bus is None:
        from .slack_convo.bus import Bus
        bus = Bus()
    writer = getattr(bus, "record_seeded_ops_fix", None)
    if not callable(writer):
        raise SeedError("bus has no seeded ops-fix writer")
    stored, duplicate = writer(row)
    return {"ticket": stored, "duplicate": bool(duplicate),
            "source_event_id": seed["source_event_id"]}
