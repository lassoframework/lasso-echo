#!/usr/bin/env python3
"""Read-only configuration/storage/optional owner snapshot readback; no writes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.forward_media_generated_issuer import configured_lane, checked_snapshot
from agent.forward_media_generated_prepare import GenerationRequest, GeneratedReceiptHold


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--role', choices=['issuer', 'verifier'], default='verifier')
    p.add_argument('--request', help='Trusted owner request JSON; enables snapshot query')
    args = p.parse_args()
    try:
        lane = configured_lane(args.role)
        checks = ['independent_registry_signature', 'registry_approved_destination_and_roles',
                  'storage_versioning_readback', 'storage_object_lock_readback']
        if args.request:
            request = GenerationRequest(**json.loads(Path(args.request).read_text()))
            checked_snapshot(lane.owner, request)
            checks.append('current_owner_snapshot_readback')
        print(json.dumps({'ok': True, 'checks': checks, 'issuance_verified': False,
            'live_owner_integration_verified': False}))
        return 0
    except GeneratedReceiptHold as e:
        print(json.dumps({'ok': False, 'hold': str(e), 'issuance_verified': False}))
        return 2
    except Exception:
        print(json.dumps({'ok': False, 'hold': 'generated_preflight_unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
