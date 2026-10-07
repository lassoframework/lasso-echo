"""Owner-only one-packet CLI entry point for ForwardMediaOwnerPersistence.

This is the ONLY operational caller of the owner persistence adapter. It must
run in a dedicated owner-credential process (FORWARD_MEDIA_OWNER_DSN and
FORWARD_MEDIA_OWNER_ROLE set, no publisher/service credentials — enforced by
agent.forward_media_owner.check_environment, reused unchanged).

It accepts exactly ONE local JSON packet (schema_version=1) describing ONE
explicit tenant/asset, then:

- reads the actual hosted source bytes through the trusted HostedObjectReader
  (packet-supplied hashes/lengths are REJECTED, never trusted);
- builds OriginalRegistration from those real bytes via register_original;
- builds HistoryClearance via clear_history (which enforces the draft's
  fresh-production receipt requirement for cleared_unused);
- reads the actual hosted image/optional thumbnail bytes and builds the
  RenderManifest via build_render_manifest.

Scope restriction of this initial entry point: only ``same_object`` and
exact-byte ``rehost`` operations are accepted. ``same_object`` packets must
not carry a thumbnail at all; for ``rehost`` a thumbnail, if present, must be
the exact source bytes. Transformed renders
(``render``/``reburn``) are HELD (refused with reason operation_held) until
controlled-renderer verification is operational.

Clearance policy: ``cleared_unused`` requires BOTH an explicit genuine
fresh-production receipt ref (production_evidence_ref) AND an independent
fleet-history audit ref (history_evidence_ref) in the owner packet. Old or
unknown-history assets are NEVER auto-upgraded from used_count=0, a URL, or
any other inference — the decision must be an explicit owner input, and
hold_uncertain/hold_used remain valid inputs. used_count is not an accepted
packet field at all.

IMPORTANT: all *_evidence_ref values are owner-reviewed inputs. This CLI
validates their shape only; it does NOT cryptographically verify the receipts
or audits they reference.

Persistence happens ONLY when --apply is explicitly passed; the default is
read/validate/dry-run with no database write. Owner DSN, URLs, raw packet
contents and R2 credentials are never exposed in logs or errors: all failures
surface as static reason codes. The owner connection is closed on every path.
This module has no import side effects.
"""
import argparse
import json
import sys

SCHEMA_VERSION = 1

# Static failure reason codes. Never pair these with exception text, URLs,
# DSNs or packet contents in output.
REASONS = (
    'env_guard',                # publisher creds present / owner DSN or role missing
    'packet_unreadable',        # packet file missing or not valid JSON object
    'schema_version_unsupported',
    'field_forbidden',          # caller supplied fingerprints/lengths/digests/used_count
    'field_required',           # missing/blank required packet field
    'decision_invalid',
    'operation_invalid',
    'operation_held',           # render/reburn held pending controlled-renderer verification
    'fresh_receipts_required',  # cleared_unused without both required refs
    'source_read_failed',
    'image_read_failed',
    'thumbnail_read_failed',
    'rehost_bytes_mismatch',    # rehost bytes differ from the original bytes
    'preparation_invalid',
    'persistence_failed',
    'uncertain_commit',
)

# Packet fields the CLI must never trust: bytes-derived facts are computed
# from hosted bytes only, and used_count never influences clearance.
_FORBIDDEN_PACKET_FIELDS = frozenset({
    'source_fingerprint', 'source_length', 'image_fingerprint', 'image_length',
    'thumbnail_fingerprint', 'thumbnail_length', 'manifest_digest', 'used_count',
})
_DECISIONS = ('cleared_unused', 'hold_uncertain', 'hold_used')
_ALLOWED_OPERATIONS = ('same_object', 'rehost')
_HELD_OPERATIONS = ('render', 'reburn')


class PacketError(ValueError):
    """Packet validation failure; carries only a static reason code."""

    def __init__(self, reason):
        if reason not in REASONS:
            raise ValueError('unknown reason code')
        self.reason = reason
        super().__init__(reason)


def _require_string(packet, name):
    value = packet.get(name)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise PacketError('field_required')
    return value


def load_packet(path):
    """Read and structurally validate the single local JSON packet."""
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            packet = json.load(handle)
    except (OSError, ValueError) as exc:
        raise PacketError('packet_unreadable') from exc
    if not isinstance(packet, dict):
        raise PacketError('packet_unreadable')
    return validate_packet(packet)


def validate_packet(packet):
    """Validate one explicit tenant/asset packet. Returns a cleaned copy."""
    forbidden = sorted(_FORBIDDEN_PACKET_FIELDS.intersection(packet))
    if forbidden:
        raise PacketError('field_forbidden')
    if packet.get('schema_version') != SCHEMA_VERSION:
        raise PacketError('schema_version_unsupported')
    cleaned = {
        'schema_version': SCHEMA_VERSION,
        'tenant_id': _require_string(packet, 'tenant_id'),
        'source_asset_id': _require_string(packet, 'source_asset_id'),
        'source_url': _require_string(packet, 'source_url'),
        'registry_evidence_ref': _require_string(packet, 'registry_evidence_ref'),
        'render_evidence_ref': _require_string(packet, 'render_evidence_ref'),
    }
    decision = packet.get('decision')
    if decision not in _DECISIONS:
        raise PacketError('decision_invalid')
    cleaned['decision'] = decision
    operation = packet.get('operation')
    if operation in _HELD_OPERATIONS:
        # Transformed renders stay held until controlled-renderer verification
        # is operational; this entry point never prepares or persists them.
        raise PacketError('operation_held')
    if operation not in _ALLOWED_OPERATIONS:
        raise PacketError('operation_invalid')
    cleaned['operation'] = operation
    history_ref = packet.get('history_evidence_ref')
    if decision == 'cleared_unused':
        # Explicit owner inputs only: a genuine fresh-production receipt AND an
        # independent fleet-history audit. No auto-upgrade from used_count=0,
        # URL shape or any other inference.
        production_ref = packet.get('production_evidence_ref')
        if not isinstance(production_ref, str) or not production_ref.strip():
            raise PacketError('fresh_receipts_required')
        if not isinstance(history_ref, str) or not history_ref.strip():
            raise PacketError('fresh_receipts_required')
        cleaned['production_evidence_ref'] = production_ref.strip()
    cleaned['history_evidence_ref'] = _require_string(packet, 'history_evidence_ref')
    if operation == 'same_object':
        cleaned['image_url'] = cleaned['source_url']
    else:
        cleaned['image_url'] = _require_string(packet, 'image_url')
    thumbnail_url = packet.get('thumbnail_url')
    cleaned['thumbnail_url'] = None
    if thumbnail_url is not None:
        if operation == 'same_object':
            # The attester classifies any same_object row carrying a
            # thumbnail at a different URL/bytes as a render/rehost, which
            # would mismatch the SQL manifest operation. Refuse all
            # thumbnails for same_object (the safe closed choice).
            raise PacketError('preparation_invalid')
        cleaned['thumbnail_url'] = _require_string(packet, 'thumbnail_url')
    if (operation == 'rehost' and cleaned['image_url'] == cleaned['source_url']
            and cleaned['thumbnail_url'] in (None, cleaned['source_url'])):
        # The attester classifies this URL tuple as same_object, so a rehost
        # manifest would never match its independently derived operation.
        raise PacketError('preparation_invalid')
    render_recipe = packet.get('render_recipe')
    if render_recipe is not None and not isinstance(render_recipe, dict):
        raise PacketError('packet_unreadable')
    cleaned['render_recipe'] = render_recipe
    return cleaned


def build_tuples(packet, reader):
    """Build (OriginalRegistration, HistoryClearance, RenderManifest) from bytes.

    All fingerprints and lengths come from the real hosted bytes read through
    the trusted reader; nothing is taken from the packet.
    """
    from agent.forward_media_prepare import (
        PreparationError,
        build_render_manifest,
        clear_history,
        register_original,
    )
    try:
        source_bytes = reader.read(packet['source_url'])
    except Exception as exc:
        raise PacketError('source_read_failed') from exc
    if not isinstance(source_bytes, (bytes, bytearray)) or not source_bytes:
        raise PacketError('source_read_failed')
    try:
        original = register_original(
            packet['tenant_id'], packet['source_asset_id'], packet['source_url'],
            bytes(source_bytes), packet['registry_evidence_ref'])
        clearance = clear_history(
            original, packet['decision'], packet['history_evidence_ref'],
            production_evidence_ref=packet.get('production_evidence_ref'))
    except PreparationError as exc:
        raise PacketError('preparation_invalid') from exc
    if packet['operation'] == 'same_object':
        image_bytes = bytes(source_bytes)
    else:
        try:
            image_bytes = reader.read(packet['image_url'])
        except Exception as exc:
            raise PacketError('image_read_failed') from exc
        if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
            raise PacketError('image_read_failed')
        image_bytes = bytes(image_bytes)
    thumbnail_bytes = None
    if packet['thumbnail_url'] is not None:
        try:
            thumbnail_bytes = reader.read(packet['thumbnail_url'])
        except Exception as exc:
            raise PacketError('thumbnail_read_failed') from exc
        if not isinstance(thumbnail_bytes, (bytes, bytearray)) or not thumbnail_bytes:
            raise PacketError('thumbnail_read_failed')
        thumbnail_bytes = bytes(thumbnail_bytes)
        if thumbnail_bytes != bytes(source_bytes):
            # The attester's rehost classification requires the thumbnail to
            # be the exact source bytes; a transformed thumbnail would
            # mismatch the SQL manifest operation.
            raise PacketError('preparation_invalid')
    try:
        manifest = build_render_manifest(
            original, packet['image_url'], image_bytes, packet['operation'],
            packet['render_evidence_ref'], thumbnail_url=packet['thumbnail_url'],
            thumbnail_bytes=thumbnail_bytes, render_recipe=packet['render_recipe'])
    except PreparationError as exc:
        raise PacketError('preparation_invalid') from exc
    if packet['operation'] == 'rehost' and (
            manifest.image_fingerprint != original.source_fingerprint
            or manifest.image_length != original.source_length):
        # Exact-byte rehost only: the delivered object must be byte-identical
        # to the registered original; anything else is a transformed render.
        raise PacketError('rehost_bytes_mismatch')
    return original, clearance, manifest


def _close(persistence):
    close = getattr(persistence, 'close', None)
    if callable(close):
        try:
            close()
        except Exception:
            pass  # best-effort close; never leak a stack trace
        return
    conn = getattr(persistence, '_conn', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def run(argv=None, *, reader_factory=None, persistence_factory=None, out=None):
    """Execute the one-packet flow. Returns (exit_code, static_result_dict)."""
    from agent import forward_media_owner
    args = _parse_args(argv)
    out = sys.stdout if out is None else out
    persistence = None
    try:
        try:
            forward_media_owner.check_environment()
        except forward_media_owner.OwnerPersistenceError as exc:
            raise PacketError('env_guard') from exc
        packet = load_packet(args.packet)
        try:
            reader = (reader_factory or forward_media_owner.HostedObjectReader)()
        except forward_media_owner.UncertainCommitError as exc:
            raise PacketError('uncertain_commit') from exc
        except Exception as exc:
            # Never print the exception: it may embed URLs or credentials.
            raise PacketError('source_read_failed') from exc
        original, clearance, manifest = build_tuples(packet, reader)
        result = {'ok': True, 'applied': False,
                  'decision': clearance.decision, 'operation': manifest.operation}
        if args.apply:
            factory = persistence_factory or (
                lambda: forward_media_owner.ForwardMediaOwnerPersistence
                .connect_from_environment(reader=reader))
            try:
                persistence = factory()
            except forward_media_owner.UncertainCommitError as exc:
                raise PacketError('uncertain_commit') from exc
            except Exception as exc:
                # Never print the exception: it may embed a DSN or credentials.
                raise PacketError('persistence_failed') from exc
            try:
                persisted = persistence.persist(original, clearance, manifest)
            except forward_media_owner.UncertainCommitError as exc:
                raise PacketError('uncertain_commit') from exc
            except Exception as exc:
                raise PacketError('persistence_failed') from exc
            if not isinstance(persisted, dict) or not isinstance(persisted.get('replayed'), bool):
                raise PacketError('persistence_failed')
            result['applied'] = True
            result['replayed'] = bool(persisted.get('replayed'))
        _emit(out, result)
        return 0, result
    except PacketError as exc:
        result = {'ok': False, 'reason': exc.reason}
        _emit(out, result)
        return 2, result
    finally:
        if persistence is not None:
            _close(persistence)


def _emit(out, result):
    # Static codes and booleans only: no URLs, DSN, credentials or packet data.
    print(json.dumps(result, sort_keys=True), file=out)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='forward_media_owner_packet',
        description='Owner-only one-packet forward media claim entry point. '
                    'Default is dry-run; --apply persists via the owner adapter.')
    parser.add_argument('--packet', required=True,
                        help='path to the single local schema_version=1 JSON packet')
    parser.add_argument('--apply', action='store_true',
                        help='persist via ForwardMediaOwnerPersistence; default dry-run')
    return parser.parse_args(argv)


def main():  # pragma: no cover - thin console wrapper
    code, _ = run()
    raise SystemExit(code)


if __name__ == '__main__':  # pragma: no cover
    main()
