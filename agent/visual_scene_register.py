"""Atomic calendar insert plus advisory scene-candidate staging.

When ``AGENT_VISUAL_SCENE_REGISTER`` is explicitly enabled, the writer requires
``AGENT_VISUAL_GLOBAL_WRITER_PREP`` and ``AGENT_VISUAL_SCENE_CANDIDATE`` to be
explicitly enabled too. Prepared calendar rows then use one SECURITY DEFINER
RPC instead of a REST insert followed by a second registration call.
PostgreSQL stages each row's candidate before that row's
BEFORE INSERT trigger runs, and the whole batch is one statement transaction.
Any candidate or row failure therefore rolls back every candidate and calendar
row in the batch. A staged candidate remains evidence only; this module never
marks it used or writes scene occupancy.

The flag is OFF by default. The normal REST calendar insert remains untouched
while it is off. The target SQL is an unapplied draft; this path must not be
enabled until that draft and its prerequisite visual migrations are applied and
separately accepted.
"""

import os
import re
import uuid


PHASH_RE = re.compile(r"^[0-9a-f]{16}$")
FINGERPRINT_RE = re.compile(r"^md5:[0-9a-f]{32}$")

_ACTOR = "visual_writer_prepare"
_DEFAULT_EVIDENCE_REF = "visual_writer_prepare:candidate_scene_evidence"
_RPC = "visual_scene_insert_calendar_batch"


class SceneRegistrationError(Exception):
    """The atomic candidate/calendar write could not be safely completed."""


def enabled() -> bool:
    """Explicit-on only; an explicitly requested route never degrades to REST."""
    try:
        from . import config
        return config.visual_scene_register_flag() is True
    except Exception as exc:  # noqa: BLE001 - distinguish OFF from broken ON
        raw = (os.environ.get("AGENT_VISUAL_SCENE_REGISTER", "") or "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            raise SceneRegistrationError(
                "atomic scene registration could not read its explicit runtime flag"
            ) from exc
        return False


def require_prerequisites() -> None:
    """Fail closed unless every prerequisite for the atomic route is armed.

    Registration replaces the persistence boundary. Once it is requested, a
    missing preparation or candidate-emission flag must never degrade to the
    ordinary REST insert. Candidate emission must be explicitly true; its
    historical ambiguous-as-armed behavior is not sufficient for this route.
    """
    if not enabled():
        return
    try:
        from . import config, visual_writer_prepare
        prepared = visual_writer_prepare.enabled()
        candidate = config.visual_scene_candidate_flag()
    except Exception as exc:  # noqa: BLE001 - an armed writer must fail closed
        raise SceneRegistrationError(
            "atomic scene registration could not verify its runtime prerequisites"
        ) from exc
    missing = []
    if not prepared:
        missing.append("AGENT_VISUAL_GLOBAL_WRITER_PREP=true")
    if candidate is not True:
        missing.append("AGENT_VISUAL_SCENE_CANDIDATE=true")
    if missing:
        raise SceneRegistrationError(
            "AGENT_VISUAL_SCENE_REGISTER=true requires " + " and ".join(missing)
        )


def _nonblank(value):
    return isinstance(value, str) and bool(value.strip())


def row_delivered_object(row):
    """Mirror ``visual_scene_row_delivered_object`` for the outbound contract."""
    img = row.get("image_url").strip() if _nonblank(row.get("image_url")) else None
    thumb = (row.get("thumbnail_url").strip()
             if _nonblank(row.get("thumbnail_url")) else None)
    if thumb is not None and thumb != img:
        return ("poster", thumb)
    if img is not None:
        return ("display", img)
    return None


def resolve_candidate_object(candidate, role, exact_url):
    """Return one unambiguous entry for the row's exact displayed object."""
    if not isinstance(candidate, dict):
        return None
    objects = candidate.get("objects")
    if not isinstance(objects, list):
        return None
    matches = []
    for entry in objects:
        if not isinstance(entry, dict) or entry.get("exact_url") != exact_url:
            continue
        entry_role = entry.get("role")
        if role == "poster":
            if entry_role != "poster":
                continue
        elif entry_role not in ("delivered", "same_object"):
            continue
        if entry.get("stageable") is not True:
            continue
        phash = entry.get("phash")
        fingerprint = entry.get("fingerprint")
        if (not isinstance(phash, str) or not PHASH_RE.fullmatch(phash)
                or not isinstance(fingerprint, str)
                or not FINGERPRINT_RE.fullmatch(fingerprint)):
            raise SceneRegistrationError(
                "stageable candidate for the exact displayed object has invalid identity"
            )
        matches.append(entry)
    if not matches:
        return None
    identities = {(entry["phash"], entry["fingerprint"]) for entry in matches}
    if len(identities) != 1:
        raise SceneRegistrationError(
            "conflicting staged scene candidates for the exact displayed object"
        )
    return matches[0]


def _registration(row, candidate, index):
    """Normalize one candidate while retaining all exact-byte binding fields."""
    delivered = row_delivered_object(row)
    if candidate is None:
        if delivered is not None:
            raise SceneRegistrationError(
                f"calendar row {index}: media row has no scene_candidate while "
                "scene registration is armed")
        return None
    if delivered is None:
        raise SceneRegistrationError(
            f"calendar row {index}: scene candidate has no displayed object")
    if (candidate.get("kind") != "visual_scene_candidate"
            or candidate.get("stage") != "candidate"
            or candidate.get("usage_claimed") is not False
            or candidate.get("counts_as_use") is not False):
        raise SceneRegistrationError(
            f"calendar row {index}: scene candidate is not staging-only evidence")
    role, exact_url = delivered
    entry = resolve_candidate_object(candidate, role, exact_url)
    if entry is None:
        raise SceneRegistrationError(
            f"calendar row {index}: no stageable '{role}' candidate for the "
            "exact displayed object")
    tenant = candidate.get("tenant_id")
    group_key = candidate.get("group_key")
    if not _nonblank(tenant) or not _nonblank(group_key):
        raise SceneRegistrationError(
            f"calendar row {index}: scene candidate has no tenant or visual group")
    if row.get("visual_group_key") != group_key:
        raise SceneRegistrationError(
            f"calendar row {index}: scene candidate does not match the row visual group")
    evidence = {
        "verified_bytes": entry["fingerprint"],
        "tenant_id": tenant,
        "group_key": group_key,
        "object_role": role,
        "exact_url": exact_url,
        "phash": entry["phash"],
        "byte_length": entry.get("byte_length"),
        "scene_fingerprint": entry.get("scene_fingerprint"),
        "observed_by": candidate.get("observed_by") or _ACTOR,
        "evidence_ref": candidate.get("evidence_ref") or _DEFAULT_EVIDENCE_REF,
    }
    return {
        "kind": "visual_scene_candidate",
        "stage": "candidate",
        "usage_claimed": False,
        "counts_as_use": False,
        "tenant_id": tenant,
        "group_key": group_key,
        "object_role": role,
        "exact_url": exact_url,
        "phash": entry["phash"],
        "fingerprint": entry["fingerprint"],
        "actor": _ACTOR,
        "evidence": evidence,
    }


def build_items(rows, scene_candidates):
    """Build the RPC array without mutating calendar rows or candidates."""
    if not isinstance(rows, list) or not isinstance(scene_candidates, list):
        raise SceneRegistrationError("atomic scene write needs row and candidate lists")
    if len(rows) != len(scene_candidates) or not rows:
        raise SceneRegistrationError("atomic scene write has mismatched or empty inputs")
    items = []
    for index, (row, candidate) in enumerate(zip(rows, scene_candidates)):
        if not isinstance(row, dict):
            raise SceneRegistrationError(f"calendar row {index}: payload is invalid")
        items.append({"calendar_row": dict(row),
                      "candidate": _registration(row, candidate, index)})
    return items


def _rpc(store, arguments):
    response = store._client().post(
        store._rest("rpc/" + _RPC),
        headers=store._headers({"Content-Type": "application/json"}),
        json=arguments,
        timeout=30,
    )
    if response.status_code >= 400:
        raise SceneRegistrationError(
            f"atomic scene calendar RPC failed ({response.status_code})")
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise SceneRegistrationError(
            "atomic scene calendar RPC returned invalid JSON") from exc


def insert_batch(store, account_key, rows, scene_candidates):
    """Atomically register bound candidates and insert their calendar rows.

    The SQL function returns the input index because the BEFORE trigger may
    legitimately persist a held status instead of the input status. Output
    verification checks the complete index set, UUIDs, tenant isolation, and
    candidate ids without mistaking a durable hold for failure.
    """
    if not _nonblank(account_key):
        raise SceneRegistrationError("atomic scene write needs an account key")
    items = build_items(rows, scene_candidates)
    result = _rpc(store, {"p_account_key": account_key, "p_items": items,
                          "p_actor": _ACTOR})
    if not isinstance(result, list) or len(result) != len(items):
        raise SceneRegistrationError("atomic scene calendar RPC returned an incomplete batch")
    ordered = [None] * len(items)
    seen_ids = set()
    for wrapper in result:
        if not isinstance(wrapper, dict):
            raise SceneRegistrationError("atomic scene calendar RPC returned an invalid item")
        index = wrapper.get("input_index")
        row = wrapper.get("row")
        if (not isinstance(index, int) or isinstance(index, bool)
                or index < 0 or index >= len(items) or ordered[index] is not None
                or not isinstance(row, dict)
                or str(row.get("gym_id")) != str(account_key)):
            raise SceneRegistrationError("atomic scene calendar RPC returned an unverified item")
        try:
            row_id = str(uuid.UUID(str(row.get("id"))))
        except (TypeError, ValueError, AttributeError) as exc:
            raise SceneRegistrationError(
                "atomic scene calendar RPC returned a row without a UUID") from exc
        if row_id in seen_ids:
            raise SceneRegistrationError("atomic scene calendar RPC returned a duplicate row")
        seen_ids.add(row_id)
        candidate_id = wrapper.get("candidate_id")
        binding = wrapper.get("candidate_binding")
        if items[index]["candidate"] is None:
            if candidate_id is not None or binding is not None:
                raise SceneRegistrationError("non-media row unexpectedly staged a scene candidate")
        else:
            try:
                normalized_candidate_id = str(uuid.UUID(str(candidate_id)))
            except (TypeError, ValueError, AttributeError) as exc:
                raise SceneRegistrationError(
                    "atomic scene calendar RPC did not return a candidate UUID") from exc
            expected = items[index]["candidate"]
            delivered = row_delivered_object(row)
            if (not isinstance(binding, dict)
                    or binding.get("candidate_id") != normalized_candidate_id
                    or binding.get("group_key") != expected["group_key"]
                    or binding.get("object_role") != expected["object_role"]
                    or binding.get("exact_url") != expected["exact_url"]
                    or binding.get("phash") != expected["phash"]
                    or binding.get("fingerprint") != expected["fingerprint"]
                    or not isinstance(binding.get("registration_reused"), bool)
                    or row.get("visual_group_key") != expected["group_key"]
                    or delivered != (expected["object_role"], expected["exact_url"])):
                raise SceneRegistrationError(
                    "atomic scene calendar RPC did not rebind the persisted row "
                    "to its registered candidate"
                )
        held = (row.get("status") == "pending"
                and row.get("variant_status") == "archived"
                and row.get("media_not_ready_reason") == "scene_review_hold"
                and row.get("publish_claim_token") is None
                and row.get("publish_reservation_day") is None)
        disposition = wrapper.get("disposition")
        if held:
            if disposition != "scene_review_hold":
                raise SceneRegistrationError(
                    "atomic scene calendar RPC mislabeled a persisted scene hold")
        elif (disposition != "inserted"
              or row.get("media_not_ready_reason") == "scene_review_hold"):
            raise SceneRegistrationError(
                "atomic scene calendar RPC returned an invalid persisted disposition")
        ordered[index] = row
    if any(row is None for row in ordered):
        raise SceneRegistrationError("atomic scene calendar RPC omitted an input row")
    return ordered
