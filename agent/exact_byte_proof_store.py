"""Append-only durable proof sink for exact-byte immutable-media proofs.

trusted_bool_adapter requires a proof_sink that durably records each
ImmutableMediaProof BEFORE a send is authorized; any sink failure holds the
send (the adapter converts it to a False decision). This module provides two
server-owned sinks:

- LocalJsonlProofSink: append-only JSONL with flush + os.fsync on every
  record. Suitable for dev/test; Railway's ephemeral local disk is NOT a
  production-durable store.
- RpcProofSink: production sink writing through the portal store's PostgREST
  RPC wrapper to the append-only exact_byte_proof_sink_20261010 table
  (migrations/exact_byte_proof_sink_20261010.sql, UNAPPLIED until an operator
  applies it). The RPC commits inside its own transaction before returning.

Neither sink exposes update or delete paths; prior records are never touched.
Any failure raises so the adapter holds the exact-byte guard ON.
"""
import dataclasses
import json
import os

from .r2_immutable_media import ImmutableMediaProof

#: Server-pinned production RPC (never caller- or row-selectable).
PROOF_RPC = "exact_byte_proof_append_20261010"

#: Environment selectors for proof_sink_from_env. Unset/unknown fails closed.
ENV_SINK_MODE = "AGENT_EXACT_BYTE_PROOF_SINK"
ENV_LOCAL_PATH = "AGENT_EXACT_BYTE_PROOF_PATH"

#: The exact-byte send guard arming flag. When it is armed, the local JSONL
#: sink is NOT an acceptable production proof sink: durable RPC only.
ENV_SEND_GUARD = "AGENT_EXACT_BYTE_SEND_GUARD"


class ProofSinkError(RuntimeError):
    """The proof could not be durably recorded; the send must stay held."""


def serialize_proof(proof):
    """Strict JSON-safe projection of a verified proof; nothing else accepted."""
    if not isinstance(proof, ImmutableMediaProof):
        raise ProofSinkError("immutable media proof required")
    record = dataclasses.asdict(proof)
    # Round-trip proves every field is JSON-safe before any durable write.
    return json.loads(json.dumps(record, allow_nan=False))


class LocalJsonlProofSink:
    """Append-only local JSONL sink; flush + fsync on every record."""

    def __init__(self, path):
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ProofSinkError("proof sink path must be absolute")
        self._path = path

    @property
    def path(self):
        return self._path

    def __call__(self, proof):
        record = serialize_proof(proof)
        try:
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except ProofSinkError:
            raise
        except Exception:
            raise ProofSinkError("proof sink append failed") from None


class RpcProofSink:
    """Production sink through the store's commit-before-return RPC wrapper."""

    def __init__(self, store, rpc=PROOF_RPC):
        if not callable(getattr(store, "_reservation_rpc", None)):
            raise ProofSinkError("proof sink requires the portal store RPC path")
        if rpc != PROOF_RPC:
            raise ProofSinkError("proof sink RPC is server-pinned")
        self._store = store
        self._rpc = rpc

    def __call__(self, proof):
        record = serialize_proof(proof)
        try:
            result = self._store._reservation_rpc(self._rpc, {"p_proof": record}, timeout=30)
        except Exception:
            # Outcome UNKNOWN: the row may be committed. That is safe here --
            # the sink is append-only and replay inserts a duplicate record,
            # never overwrites. The send stays held either way.
            raise ProofSinkError("proof sink RPC unavailable") from None
        if result is not True:
            raise ProofSinkError("proof sink RPC refused the proof")


def proof_sink_from_env(environ=None, *, store=None):
    """Build the env-selected sink or return None (caller holds the guard).

    ``local`` requires AGENT_EXACT_BYTE_PROOF_PATH (absolute). ``rpc``
    requires the portal store. Anything missing or malformed yields None.
    """
    environ = os.environ if environ is None else environ
    try:
        mode = (environ.get(ENV_SINK_MODE, "") or "").strip().lower()
        if mode == "local":
            if (environ.get(ENV_SEND_GUARD, "") or "").strip().lower() in (
                    "1", "true", "yes", "on"):
                # Match delivered_byte_send_guard.enabled() arming forms.
                # Production guard armed: a local JSONL file is not durable
                # (ephemeral disk); only the commit-before-return RPC sink is.
                return None
            return LocalJsonlProofSink((environ.get(ENV_LOCAL_PATH, "") or "").strip())
        if mode == "rpc":
            if store is None:
                return None
            return RpcProofSink(store)
        return None
    except Exception:
        return None
