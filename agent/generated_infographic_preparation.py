"""Fresh Astra candidates for the photo-depleted lane; never calendar authority.

An owner snapshot supplies approved copy, verified palette and complete inventory
and history. SQLite persists one job per exact binding before any provider I/O.
A lost provider response holds the job for reconciliation rather than issuing a
second image. Final acceptance/reservation belongs to the database owner lane.
"""
from __future__ import annotations

import base64
from datetime import date
import hashlib
import io
import json
import os
import re
import sqlite3
import uuid

BINDING_FIELDS = ("gym_id", "local_date", "logical_post_id", "copy_revision",
                  "palette_revision", "inventory_revision", "history_revision")
POLICY = "gym-infographic-copy-palette-v1"
MAX_BYTES = 134217728


class PreparationHold(RuntimeError):
    """Static diagnostic only; no provider body, credentials or private data."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validated_binding(request):
    if not isinstance(request, dict) or set(request) != set(BINDING_FIELDS):
        raise PreparationHold("generated_request_invalid")
    result = dict(request)
    if any(not isinstance(v, str) or not v.strip() for v in result.values()):
        raise PreparationHold("generated_request_invalid")
    try:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", result["gym_id"]):
            raise ValueError()
        if date.fromisoformat(result["local_date"]).isoformat() != result["local_date"]:
            raise ValueError()
        if str(uuid.UUID(result["logical_post_id"])) != result["logical_post_id"]:
            raise ValueError()
    except (ValueError, TypeError):
        raise PreparationHold("generated_request_invalid") from None
    return result


def checked_snapshot(request, snapshot):
    if (not isinstance(snapshot, dict)
            or any(snapshot.get(k) != request[k] for k in BINDING_FIELDS)):
        raise PreparationHold("generated_snapshot_binding_changed")
    if snapshot.get("photo_inventory_complete") is not True:
        raise PreparationHold("generated_photo_inventory_uncertain")
    if type(snapshot.get("eligible_photo_count")) is not int:
        raise PreparationHold("generated_photo_inventory_uncertain")
    if snapshot["eligible_photo_count"] != 0:
        raise PreparationHold("generated_photo_available")
    if snapshot.get("history_complete") is not True:
        raise PreparationHold("generated_history_uncertain")
    if snapshot.get("copy_approved") is not True:
        raise PreparationHold("generated_copy_unapproved")
    copy = snapshot.get("copy")
    if (not isinstance(copy, dict) or set(copy) != {"headline", "facts", "cta", "footer"}
            or any(not isinstance(copy[k], str) for k in ("headline", "cta", "footer"))
            or not copy["headline"].strip()
            or not isinstance(copy["facts"], list) or not copy["facts"]
            or any(not isinstance(v, str) or not v.strip() for v in copy["facts"])):
        raise PreparationHold("generated_copy_invalid")
    # Reject unsupported display punctuation up front, never silently rewrite
    # approved words then claim that the generated card matches the source.
    if any(c in text for text in [copy["headline"], *copy["facts"], copy["cta"], copy["footer"]]
           for c in ("-", "–", "—", ":", ";")):
        raise PreparationHold("generated_copy_style_invalid")
    palette = snapshot.get("palette")
    from .astra_prompt import build_verified_gym_content_brief
    try:
        brief = build_verified_gym_content_brief(request["gym_id"], copy, palette)
    except (ValueError, TypeError):
        raise PreparationHold("generated_palette_unverified") from None
    return copy, palette, brief


class SQLiteGenerationJobs:
    """Durable execution journal, shared by workers using the same configured file.

    Short BEGIN IMMEDIATE transactions fence provider execution; network I/O is
    outside the transaction. Ambiguous jobs are retained until reconciled. This
    journal conveys no trusted source approval or final DB reservation.
    """
    def __init__(self, path):
        self.path = os.fspath(path)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS generated_jobs ("
                        "job_id TEXT PRIMARY KEY, binding TEXT NOT NULL, "
                        "state TEXT NOT NULL, response TEXT, candidate TEXT)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def start(self, job_id, binding):
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT binding,state,response,candidate FROM generated_jobs "
                              "WHERE job_id=?", (job_id,)).fetchone()
            if row:
                if row[0] != canonical(binding):
                    raise PreparationHold("generated_job_binding_changed")
                return {"state": row[1], "response": json.loads(row[2]) if row[2] else None,
                        "candidate": json.loads(row[3]) if row[3] else None}
            con.execute("INSERT INTO generated_jobs(job_id,binding,state) VALUES (?,?,?)",
                        (job_id, canonical(binding), "generating"))
            return {"state": "new"}

    def provider_completed(self, job_id, response):
        with self._connect() as con:
            cur = con.execute("UPDATE generated_jobs SET state='reviewing', response=? "
                              "WHERE job_id=? AND state='generating'",
                              (canonical(response), job_id))
            if cur.rowcount != 1:
                raise PreparationHold("generated_job_state_changed")

    def finish(self, job_id, candidate):
        with self._connect() as con:
            cur = con.execute("UPDATE generated_jobs SET state='prepared', candidate=? "
                              "WHERE job_id=? AND state='reviewing'",
                              (canonical(candidate), job_id))
            if cur.rowcount != 1:
                raise PreparationHold("generated_job_state_changed")


class AstraOriginalProvider:
    """Existing Astra HTTP route with original inline bytes kept before resizing."""
    def __init__(self, api_key, transport=None):
        from .image_engine import AstraImageEngine
        self.engine = AstraImageEngine(api_key, transport=transport)

    def create(self, brief, job_id):
        from .image_engine import astra_image_model
        status, body = self.engine._post({
            "model": "gpt-6-astra", "store": True,
            "metadata": {"echo_generation_job_id": job_id}, "input": brief,
            "tools": [{"type": "image_generation", "model": astra_image_model(),
                       "action": "generate", "size": "1024x1280"}],
            "tool_choice": {"type": "image_generation"},
        })
        if status != 200:
            raise PreparationHold("generated_provider_unavailable")
        try:
            return json.loads(body)
        except (ValueError, TypeError):
            raise PreparationHold("generated_provider_response_invalid") from None


def original_bytes(response, job_id):
    try:
        if (response.get("status") != "completed"
                or response.get("model") != "gpt-6-astra"
                or response.get("metadata", {}).get("echo_generation_job_id") != job_id
                or not isinstance(response.get("id"), str) or not response["id"].strip()):
            raise ValueError()
        outputs = [x for x in response["output"] if x.get("type") == "image_generation_call"]
        if len(outputs) != 1 or not str(outputs[0].get("id") or "").strip():
            raise ValueError()
        data = base64.b64decode(outputs[0]["result"], validate=True)
        if not data or len(data) > MAX_BYTES:
            raise ValueError()
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or image.size != (1024, 1280):
                raise ValueError()
            image.load()
        return data, outputs[0]["id"]
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        raise PreparationHold("generated_original_invalid") from None


class GymPaletteReviewer:
    """Add an explicit palette check to the existing independent pixel rubric."""
    def __init__(self, reviewer, palette):
        self.reviewer, self.palette = reviewer, palette

    def ask_image(self, data, question):
        raw = self.reviewer.ask_image(data, question +
            " PALETTE REQUIREMENT overrides the general palette latitude above. "
            "Use actual pixels to verify this gym's exact approved colors " +
            canonical(self.palette["colors"]) +
            ". Add palette_matches boolean to the JSON. False or uncertainty blocks.")
        result = json.loads(raw)
        if result.get("palette_matches") is not True:
            raise PreparationHold("generated_palette_review_failed")
        return canonical(result)


def prepare_candidate(request, snapshot, *, jobs, provider, reviewer, storage=None,
                      enabled=False):
    """Return a verified candidate dict or HOLD; does not stage, approve or send.

    Call with the owner's current snapshot on every retry. Final owner rechecks
    all revisions/history under its reservation locks, including late photos.
    Provider calls only occur for a new durable job. Review/storage may retry on
    the already journaled original, never generate another image for that job.
    """
    if not enabled:
        return {"ok": False, "held": True, "reason": "generated_preparation_disabled"}
    try:
        request = validated_binding(request)
        # Freeze mutable caller structures before remote I/O. A changed palette
        # or copy may not mutate the pixels' binding while review is in flight.
        snapshot = json.loads(canonical(snapshot))
        copy, palette, brief = checked_snapshot(request, snapshot)
        binding = {**request, "copy_digest": digest(copy), "palette_digest": digest(palette),
                   "review_policy_id": POLICY}
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "echo-astra:" + canonical(binding)))
        job = jobs.start(job_id, binding)
        if job["state"] == "generating":
            raise PreparationHold("generated_execution_pending_reconciliation")
        if job["state"] == "prepared":
            candidate = job["candidate"]
            # Changed object bytes invalidate even an otherwise identical retry.
            from .media_host import host_generated_original
            data, output_id = original_bytes(job["response"], job_id)
            validate_candidate(candidate, data)
            if (candidate["provider_response_id"] != job["response"]["id"]
                    or candidate["provider_output_id"] != output_id
                    or any(candidate[k] != v for k, v in binding.items())):
                raise PreparationHold("generated_job_binding_changed")
            receipt = host_generated_original(data, request["gym_id"], client=storage)
            if not receipt or any(candidate[k] != v for k, v in receipt.items()):
                raise PreparationHold("generated_storage_readback_failed")
            return {"ok": True, "candidate": candidate}
        if job["state"] == "new":
            response = provider.create(brief, job_id)
            # Journal the full provider original before reviewing or hosting.
            jobs.provider_completed(job_id, response)
        elif job["state"] == "reviewing":
            response = job["response"]
        else:
            raise PreparationHold("generated_job_state_invalid")
        data, output_id = original_bytes(response, job_id)
        from .visual_scene import scene_fingerprint
        phash = scene_fingerprint(data)
        if not phash:
            raise PreparationHold("generated_perceptual_hash_unavailable")
        from .infographic_review import evaluate
        grade = evaluate(data, headline=copy["headline"], facts=copy["facts"],
                         cta=copy["cta"], footer=copy["footer"], surface="feed",
                         vision_client=GymPaletteReviewer(reviewer, palette))
        review_id = str(getattr(reviewer, "response_id", "") or "")
        if (not grade.passed or grade.status != "PASS" or not review_id
                or review_id == response["id"]):
            raise PreparationHold("generated_automated_review_failed")
        from .media_host import host_generated_original
        receipt = host_generated_original(data, request["gym_id"], client=storage)
        if not receipt:
            raise PreparationHold("generated_storage_readback_failed")
        candidate = {"schema_version": 1, "source_type": "generated_astra_infographic",
                     **binding, "job_id": job_id, "provider": "astra", "model": "gpt-6-astra",
                     "provider_response_id": response["id"], "provider_output_id": output_id,
                     "original_sha256": hashlib.sha256(data).hexdigest(),
                     "original_md5": hashlib.md5(data).hexdigest(), "original_length": len(data),
                     "original_phash": phash, "width": 1024, "height": 1280,
                     "review_response_id": review_id, **receipt}
        validate_candidate(candidate, data)
        jobs.finish(job_id, candidate)
        return {"ok": True, "candidate": candidate}
    except PreparationHold as exc:
        return {"ok": False, "held": True, "reason": str(exc)}
    except Exception:
        # A transport/process exception after job claim is ambiguous. Preserve
        # the generating journal; no second provider call may occur on retry.
        return {"ok": False, "held": True, "reason": "generated_preparation_unavailable"}


def validate_candidate(candidate, data=None):
    """Validate shape and optional exact original bytes; conveys no owner grant."""
    fields = {"schema_version", "source_type", *BINDING_FIELDS, "copy_digest", "palette_digest",
              "review_policy_id", "job_id", "provider", "model", "provider_response_id",
              "provider_output_id", "original_sha256", "original_md5", "original_length",
              "original_phash", "width", "height", "review_response_id", "storage_key",
              "original_url", "storage_readback_sha256"}
    try:
        if not isinstance(candidate, dict) or set(candidate) != fields:
            raise ValueError()
        request = validated_binding({k: candidate[k] for k in BINDING_FIELDS})
        if (type(candidate["schema_version"]) is not int or candidate["schema_version"] != 1
                or candidate["source_type"] != "generated_astra_infographic"
                or candidate["provider"] != "astra" or candidate["model"] != "gpt-6-astra"
                or candidate["review_policy_id"] != POLICY):
            raise ValueError()
        for field in ("copy_digest", "palette_digest", "original_sha256", "storage_readback_sha256"):
            if not isinstance(candidate[field], str) or not re.fullmatch(r"[0-9a-f]{64}", candidate[field]):
                raise ValueError()
        if (not isinstance(candidate["original_md5"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", candidate["original_md5"])):
            raise ValueError()
        from .visual_scene import normalize_scene, scene_fingerprint
        if normalize_scene(candidate["original_phash"]) != candidate["original_phash"]:
            raise ValueError()
        if any(not isinstance(candidate[k], str) or not candidate[k].strip()
               for k in ("provider_response_id", "provider_output_id", "review_response_id")):
            raise ValueError()
        binding = {**request, "copy_digest": candidate["copy_digest"],
                   "palette_digest": candidate["palette_digest"], "review_policy_id": POLICY}
        if candidate["job_id"] != str(uuid.uuid5(uuid.NAMESPACE_URL, "echo-astra:" + canonical(binding))):
            raise ValueError()
        if (type(candidate["original_length"]) is not int
                or not 0 < candidate["original_length"] <= MAX_BYTES
                or type(candidate["width"]) is not int or candidate["width"] != 1024
                or type(candidate["height"]) is not int or candidate["height"] != 1280
                or candidate["storage_readback_sha256"] != candidate["original_sha256"]
                or candidate["storage_key"] != f"echo-generated-originals/{request['gym_id']}/{candidate['original_sha256']}.png"):
            raise ValueError()
        from .media_host import public_url_for
        from urllib.parse import urlsplit
        if (candidate["original_url"] != public_url_for(candidate["storage_key"])
                or urlsplit(candidate["original_url"]).scheme != "https"):
            raise ValueError()
        if data is not None:
            if (not isinstance(data, bytes) or len(data) != candidate["original_length"]
                    or hashlib.sha256(data).hexdigest() != candidate["original_sha256"]
                    or hashlib.md5(data).hexdigest() != candidate["original_md5"]
                    or scene_fingerprint(data) != candidate["original_phash"]):
                raise ValueError()
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG" or image.size != (candidate["width"], candidate["height"]):
                    raise ValueError()
                image.load()
    except (ValueError, TypeError, KeyError, OSError, PreparationHold):
        raise PreparationHold("generated_candidate_invalid") from None
    return dict(candidate)
