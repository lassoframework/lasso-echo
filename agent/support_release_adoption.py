"""Explicit operator adoption of reviewed external releases. Dry run by default.

No Slack send or ticket resolution lives here. A successful --write makes the
current ticket eligible for its ordinary outbox only after real release checks,
a separate review receipt and a freshly produced registered business observation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SOURCE = 'independent_codex_release_adoption'
CHECK = 'automatic_reel_and_thumbnails_ready'
_SHA = re.compile(r'[0-9a-f]{40}\Z')
_REPOS = {'lassoframework/lasso-echo':'pytest', 'lasso-framework/lasso-ops-portal':'portal-gate'}


class AdoptionRefused(ValueError):
    pass


def _command(args, *, cwd=None):
    try:
        result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=30, cwd=cwd)
        return json.loads(result.stdout)
    except Exception:
        raise AdoptionRefused('release_provider_read_unavailable') from None


def _pr_identity(url):
    parsed = urlsplit(str(url))
    match = re.fullmatch(r'/([^/]+/[^/]+)/pull/([1-9][0-9]*)', parsed.path)
    if parsed.scheme != 'https' or parsed.netloc != 'github.com' or parsed.query or parsed.fragment or not match:
        raise AdoptionRefused('invalid_release_pr')
    repo = match[1].lower()
    # Historical Echo remotes use lassoframework; portal uses LASSO-FRAMEWORK.
    if repo == 'lassoframework/lasso-ops-portal':
        repo = 'lasso-framework/lasso-ops-portal'
    if repo not in _REPOS:
        raise AdoptionRefused('unsupported_release_repository')
    return repo, int(match[2])


def verify_releases(plan, *, command=_command, http=None):
    """Read merged PR/CI and both providers now, rather than trust manifest booleans."""
    import requests
    supplied_command = command
    def command(args):
        # Linking a directory is an operator choice; this command only reads the
        # existing context and confirms its project ID before deployment reads.
        directory = plan.get('railway_directory') if args[0] == 'railway' else None
        if directory is not None and (not isinstance(directory, str) or not Path(directory).is_absolute()):
            raise AdoptionRefused('invalid_railway_context_directory')
        return (_command(args, cwd=directory) if supplied_command is _command else supplied_command(args))
    releases = plan.get('releases')
    if not isinstance(releases, list) or len(releases) != 2:
        raise AdoptionRefused('two_release_identifiers_required')
    verified = []
    seen = set()
    for item in releases:
        if not isinstance(item, dict) or set(item) != {'pr_url', 'merge_sha'} or not _SHA.fullmatch(str(item['merge_sha'])):
            raise AdoptionRefused('invalid_release_identifiers')
        repo, number = _pr_identity(item['pr_url'])
        if repo in seen:
            raise AdoptionRefused('duplicate_release_repository')
        seen.add(repo)
        pr = command(['gh', 'api', f'repos/{repo}/pulls/{number}'])
        if pr.get('merged') is not True or pr.get('merge_commit_sha') != item['merge_sha']:
            raise AdoptionRefused('release_pr_not_merged_at_expected_sha')
        head = (pr.get('head') or {}).get('sha')
        if not _SHA.fullmatch(str(head)):
            raise AdoptionRefused('release_head_unavailable')
        checks = command(['gh', 'api', f'repos/{repo}/commits/{head}/check-runs?per_page=100'])
        runs = checks.get('check_runs')
        if not isinstance(runs, list) or checks.get('total_count') != len(runs):
            raise AdoptionRefused('release_checks_incomplete')
        required = [run for run in runs if run.get('name') == _REPOS[repo]]
        if not required or any(run.get('status') != 'completed' or run.get('conclusion') != 'success' for run in required):
            raise AdoptionRefused('release_checks_not_passed')
        def includes(deployed):
            if not _SHA.fullmatch(str(deployed)):
                return False
            if deployed == item['merge_sha']:
                return True
            compare = command(['gh', 'api', f"repos/{repo}/compare/{item['merge_sha']}...{deployed}"])
            return compare.get('status') == 'ahead' and (compare.get('merge_base_commit') or {}).get('sha') == item['merge_sha']
        evidence = []
        if repo.endswith('/lasso-echo'):
            project = plan.get('railway_project_id')
            if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', str(project)):
                raise AdoptionRefused('railway_project_identity_required')
            context = command(['railway', 'status', '--json'])
            if context.get('id') != project:
                raise AdoptionRefused('railway_linked_project_mismatch')
            for service in ('echo', 'echo-intake-web'):
                deployments = command(['railway', 'deployment', 'list',
                                       '--environment', 'production', '-s', service, '--json'])
                if not isinstance(deployments, list) or not deployments:
                    raise AdoptionRefused('deployment_unavailable')
                try:
                    latest = max(deployments, key=lambda d: datetime.fromisoformat(d['createdAt'].replace('Z', '+00:00')))
                except Exception:
                    raise AdoptionRefused('deployment_order_unavailable') from None
                deployed = (latest.get('meta') or {}).get('commitHash')
                if latest.get('status') != 'SUCCESS' or not latest.get('id') or not includes(deployed):
                    raise AdoptionRefused('echo_deployment_unverified')
                evidence.append({'service':service, 'id':latest['id'], 'status':'SUCCESS', 'sha':deployed})
            health = 'https://echo-intake-web-production.up.railway.app/healthz'
        else:
            scope = 'blake-2786s-projects'
            current = command(['vercel', 'inspect', 'ops.lassoframework.com', '--format=json', '--scope', scope])
            did = current.get('id')
            if not re.fullmatch(r'dpl_[A-Za-z0-9]+', str(did)):
                raise AdoptionRefused('portal_deployment_identity_unavailable')
            deployment = command(['vercel', 'api', '/v13/deployments/' + did, '--scope', scope, '--raw'])
            deployed = (deployment.get('gitSource') or {}).get('sha')
            if (deployment.get('id') != did or deployment.get('target') != 'production'
                    or deployment.get('readyState') != 'READY'
                    or 'ops.lassoframework.com' not in (deployment.get('alias') or []) or not includes(deployed)):
                raise AdoptionRefused('portal_deployment_unverified')
            evidence.append({'id':did, 'status':'READY', 'sha':deployed, 'target':'production'})
            health = 'https://ops.lassoframework.com/sign-in'
        try:
            with (http or requests).get(health, timeout=(5, 5), stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise AdoptionRefused('release_health_unverified')
        except AdoptionRefused:
            raise
        except Exception:
            raise AdoptionRefused('release_health_unavailable') from None
        verified.append({**item, 'repo':repo, 'head_sha':head, 'ci_checks':[
            {'name':run['name'], 'id':run.get('id'), 'conclusion':run['conclusion']} for run in required],
            'deployment_check':{'verified':True, 'sha':item['merge_sha'], 'healthy':True, 'evidence':evidence}})
    return verified


def observe_worker(ticket, key, merged_sha, params, *, http=None):
    """Call the authenticated read-only observer on the authoritative worker.

    Operator hosts cannot substitute their local SQLite DB for production job
    state. The outbox separately re-runs the same registered check at dispatch.
    """
    import requests
    secret = os.environ.get('FIXER_OPS_SECRET') or ''
    if not secret:
        raise AdoptionRefused('worker_observer_credentials_unavailable')
    payload = {'schema_version':1, 'contract_version':'echo-business-evidence-v1',
        'ticket_id':ticket['id'], 'client_id':ticket['client_id'], 'request_key':key,
        'merged_sha':merged_sha, 'check_id':CHECK, 'params':params}
    try:
        with (http or requests).post('https://echo-production-3e71.up.railway.app/ops/actions/business-evidence/observe',
            json=payload, headers={'X-Fixer-Ops-Secret':secret}, timeout=(5, 45), stream=True,
            allow_redirects=False) as response:
            if response.status_code != 200:
                raise AdoptionRefused('worker_observer_unavailable')
            data = bytearray()
            for chunk in response.iter_content(4096):
                data.extend(chunk)
                if len(data) > 32768:
                    raise AdoptionRefused('worker_observer_response_invalid')
        return json.loads(data)
    except AdoptionRefused:
        raise
    except Exception:
        raise AdoptionRefused('worker_observer_unavailable') from None


def adopt_release(bus, expected, plan, review_bytes, *, write=False, release_reader=verify_releases,
                  deps=None, now=None):
    """Inspect actual sources, then optionally CAS one current request into merged."""
    from .slack_convo.outbox import _current_fixer_request_key
    from . import fixer_business_evidence as be
    now = now or datetime.now(timezone.utc)
    if (expected.get('product') != 'echo' or expected.get('source') != 'slack_conversation'
            or expected.get('status') != 'verification' or expected.get('classification') != 'code_fix'
            or expected.get('hold_tier') is not None or expected.get('escalated') is not False
            or expected.get('bot_identity') != 'echo' or not expected.get('client_id')
            or type(expected.get('request_version')) is not int
            or not str(expected.get('slack_channel_id') or '').startswith(('C', 'G'))):
        raise AdoptionRefused('ticket_not_eligible')
    if (plan.get('ticket_id') != expected.get('id') or type(plan.get('request_version')) is not int
            or plan.get('request_version') != expected['request_version']
            or plan.get('superseded_pr_url') != expected.get('fix_pr_url')
            or not be.automatic_reel_params_valid(plan.get('business_params'))):
        raise AdoptionRefused('adoption_identity_mismatch')
    limitations = plan.get('limitations')
    if (not isinstance(plan.get('client_note'), str) or not 1 <= len(plan['client_note'].strip()) <= 1500
            or not isinstance(limitations, list) or len(limitations) > 20
            or any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 500 for item in limitations)):
        raise AdoptionRefused('reviewed_client_scope_required')
    key = _current_fixer_request_key(bus, expected)
    if not key:
        raise AdoptionRefused('current_request_unavailable')
    try:
        review = json.loads(review_bytes)
        reviewed_at = datetime.fromisoformat(review['checked_at'].replace('Z', '+00:00'))
        age = (now - reviewed_at).total_seconds()
    except Exception:
        raise AdoptionRefused('independent_review_unreadable') from None
    urls = [item.get('pr_url') for item in plan.get('releases', []) if isinstance(item, dict)]
    if (type(review.get('schema_version')) is not int or review.get('schema_version') != 1
            or review.get('verified') is not True or type(review.get('request_version')) is not int
            or review.get('ticket_id') != expected['id'] or review.get('request_version') != expected['request_version']
            or review.get('request_key') != key or not 0 <= age <= 3600
            or not isinstance(review.get('reviewer_id'), str) or not review['reviewer_id'].strip()
            or not isinstance(review.get('builder_id'), str) or not review['builder_id'].strip()
            or review['reviewer_id'] == review['builder_id']
            or review.get('release_identifiers') != plan.get('releases')
            or review.get('business_params') != plan['business_params']
            or review.get('client_note') != plan['client_note']
            or review.get('limitations') != limitations
            or review.get('essential_failures') != []):
        raise AdoptionRefused('independent_review_not_current')
    releases = release_reader(plan)
    if not isinstance(releases, list) or len(releases) != 2 or {r.get('pr_url') for r in releases} != set(urls):
        raise AdoptionRefused('release_receipts_unconfirmed')
    primary = next((r for r in releases if r.get('repo') == 'lassoframework/lasso-echo'), None)
    if primary is None:
        raise AdoptionRefused('primary_release_missing')
    for receipt in releases:
        deployment = receipt.get('deployment_check') or {}
        if deployment.get('verified') is not True or deployment.get('sha') != receipt.get('merge_sha'):
            raise AdoptionRefused('release_receipts_unconfirmed')
    proof = (be.observe(CHECK, gym_key=expected['client_id'], request_key=key,
        merged_sha=primary['merge_sha'], params=plan['business_params'], ticket_id=expected['id'],
        deps=deps, now=now) if deps is not None else
        observe_worker(expected, key, primary['merge_sha'], plan['business_params']))
    try:
        age = (now - datetime.fromisoformat(proof['captured_at'].replace('Z', '+00:00'))).total_seconds()
    except Exception:
        age = None
    if (not isinstance(proof, dict) or proof.get('source') != be.SOURCE
            or proof.get('schema_version') != 1 or proof.get('check_id') != CHECK
            or proof.get('outcome') != be.VERIFIED or proof.get('verified') is not True
            or proof.get('symptom_resolved') is not True or not proof.get('evidence')
            or age is None or not -60 <= age <= 300
            or not be.binding_matches(proof, gym_key=expected['client_id'], request_key=key,
                                      merged_sha=primary['merge_sha'])
            or deps is None and proof.get('params') != plan['business_params']):
        raise AdoptionRefused('business_postcondition_not_verified')
    proof['params'] = copy.deepcopy(plan['business_params'])
    # Re-read after provider and business calls. The final atomic CAS also
    # compares both full JSON records so an independent writer is never erased.
    fresh = bus.ticket(expected['id'])
    if fresh != expected or _current_fixer_request_key(bus, fresh) != key:
        raise AdoptionRefused('ticket_changed_during_verification')
    before = copy.deepcopy(expected.get('verification_before') or {})
    history = before.setdefault('independent_release_adoptions', [])
    history.append({'source':SOURCE, 'superseded_pr_url':expected.get('fix_pr_url'),
        'superseded_verification_before':copy.deepcopy(expected.get('verification_before')),
        'superseded_verification_after':copy.deepcopy(expected.get('verification_after')),
        'adopted_at':now.isoformat()})
    adoption = {'source':SOURCE, 'reviewer_id':review['reviewer_id'], 'builder_id':review['builder_id'],
        'review_sha256':hashlib.sha256(review_bytes).hexdigest(), 'reviewed_at':review['checked_at'],
        'client_note_sha256':hashlib.sha256(plan['client_note'].encode()).hexdigest(),
        'limitations':copy.deepcopy(limitations), 'release_manifest':releases}
    after = {'phase':'independent_release_adoption', 'verifier':SOURCE, 'exit_code':0,
        'at':now.isoformat(), 'sha':primary['head_sha'], 'pr_url':primary['pr_url'],
        'independent_adoption':adoption,
        'fixer':{'request_key':key, 'merged_sha':primary['merge_sha'], 'merged_at':now.isoformat(),
            'deployment_check':{**primary['deployment_check'], 'checked_at':now.isoformat()},
            'business_postcondition':proof, 'release_manifest':releases,
            'client_note':plan['client_note'], 'adopted_source':SOURCE}}
    result = {'ok':True, 'write':write, 'ticket_id':expected['id'], 'request_version':expected['request_version'],
        'primary_pr_url':primary['pr_url'], 'review_sha256':adoption['review_sha256'], 'proof':proof}
    if write:
        updated = bus.adopt_independent_release(expected, fix_pr_url=primary['pr_url'],
            verification_before=before, verification_after=after)
        if (not isinstance(updated, dict) or updated.get('status') != 'merged'
                or updated.get('request_version') != expected['request_version']
                or updated.get('fix_pr_url') != primary['pr_url']
                or updated.get('verification_after') != after or updated.get('verification_before') != before):
            raise AdoptionRefused('adoption_cas_not_confirmed')
        result['status'] = 'merged'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--review', required=True)
    parser.add_argument('--write', action='store_true', help='make ticket eligible for ordinary notification; no direct send')
    args = parser.parse_args()
    try:
        from .slack_convo.bus import Bus
        plan = json.loads(Path(args.manifest).read_text())
        bus = Bus()
        result = adopt_release(bus, bus.ticket(plan['ticket_id']) or {}, plan, Path(args.review).read_bytes(), write=args.write)
        print(json.dumps(result))
    except Exception as exc:
        # Never print provider errors, signed URLs, or caller-supplied secrets.
        reason = str(exc) if isinstance(exc, AdoptionRefused) else type(exc).__name__
        print(json.dumps({'ok':False, 'reason':reason}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
