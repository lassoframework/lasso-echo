"""Exact operator dispatch stays isolated and retains ordinary send guards."""
from types import SimpleNamespace

import pytest

from agent.slack_convo import outbox


HOOKS = (
    '_recover_stale_claims', '_report_suppressed_current_notices',
    '_report_uncertain_outreach', '_reconcile_held_fixer',
    '_recover_route_missing_fixer', '_recover_config_missing_fixer',
    '_report_uncertain_fixer', '_reconcile_posted_fixer',
    '_reconcile_held_scan_reminders',
)


class ExactBus:
    def __init__(self, row):
        self.row = row
        self.reads = []
        self.marks = []
        self.unrelated = {'id': 'unrelated', 'delivery_status': 'ready'}

    def message(self, mid):
        self.reads.append(mid)
        return self.row

    def outbox(self, *args, **kwargs):
        pytest.fail('Exact dispatch scanned unrelated ready rows')

    def mark_message(self, mid, status, **kwargs):
        self.marks.append((mid, status))


@pytest.fixture
def exact(monkeypatch):
    monkeypatch.delenv('SUPPORT_MESSAGES_FENCE_ENABLED', raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail('Exact dispatch ran a recovery/report sweep')
    for name in HOOKS:
        monkeypatch.setattr(outbox, name, forbidden)
    row = {'id': 'target', 'delivery_status': 'ready', 'direction': 'outbound',
           'author_type': 'echo',
           'attachments': {'identity': 'echo', 'historical_receipt_recovery': True}}
    return ExactBus(row), SimpleNamespace(name='echo')


def run(bus, identity):
    def post(*args, **kwargs):
        pytest.fail('Unexpected Slack call')
    return outbox.run_once(bus, post, identity=identity, log=lambda _: None,
                           exact_message_id='target')


@pytest.mark.parametrize('change', [
    {'id': 'other'}, {'delivery_status': 'posting'}, {'delivery_status': 'held'},
    {'direction': 'inbound'}, {'author_type': 'system'}, {'attachments': {}},
    {'attachments': {'identity': 'scout'}}, {'attachments': None},
    {'attachments': {'identity': 'echo'}},
])
def test_ineligible_row_fails_closed(exact, monkeypatch, change):
    bus, identity = exact
    bus.row.update(change)
    monkeypatch.setattr(outbox, '_dispatch_one', lambda *a, **k: pytest.fail('Dispatched'))
    assert run(bus, identity)['skipped'] == 1
    assert bus.reads == ['target']
    assert bus.marks == []


def test_missing_row_fails_closed(exact):
    bus, identity = exact
    bus.row = None
    assert run(bus, identity)['skipped'] == 1


def test_read_error_fails_closed(exact):
    bus, identity = exact
    def unavailable(mid):
        raise OSError('offline')
    bus.message = unavailable
    assert run(bus, identity)['skipped'] == 1
    assert bus.marks == []


def test_exact_row_uses_normal_admission_gate(exact):
    bus, identity = exact
    bus.row['attachments'][outbox.SUPPORT_SEND_ADMISSION_KEY] = {'frozen': True}
    result = run(bus, identity)
    assert result['skipped'] == 1
    assert result['posted'] == 0
    assert bus.reads == ['target']
    assert bus.marks == []


def test_exact_row_uses_normal_error_handler(exact, monkeypatch):
    bus, identity = exact
    seen = []
    def dispatch(bus, post, row, **kwargs):
        seen.append(row['id'])
        raise RuntimeError('transport unavailable')
    monkeypatch.setattr(outbox, '_dispatch_one', dispatch)
    assert run(bus, identity)['failed'] == 1
    assert seen == ['target']
    assert bus.marks == [('target', 'failed')]
    assert bus.reads == ['target']


def test_exact_row_uses_resolution_admission_error_handler(exact, monkeypatch):
    bus, identity = exact
    held = []
    def dispatch(*args, **kwargs):
        raise outbox.SupportResolutionAdmissionError('not admitted')
    monkeypatch.setattr(outbox, '_dispatch_one', dispatch)
    monkeypatch.setattr(outbox, '_hold_support_send',
                        lambda b, row, reason, log: held.append(row['id']) or True)
    assert run(bus, identity)['held'] == 1
    assert held == ['target']
    assert bus.marks == []
