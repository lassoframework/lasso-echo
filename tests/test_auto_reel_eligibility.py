from types import SimpleNamespace
import pytest
from agent import story_candidates, story_ledger, story_sort_queue


@pytest.mark.parametrize('status', [401, 500])
def test_shared_ledger_outage_cannot_fall_back_to_empty_local(monkeypatch, status):
    monkeypatch.setattr(story_ledger, '_supabase_conf', lambda: ('https://example.invalid', 'test'))
    monkeypatch.setattr(story_ledger, '_kv_has', lambda value: False)
    http=SimpleNamespace(get=lambda *a,**k:SimpleNamespace(status_code=status))
    with pytest.raises(ValueError,match='unavailable'):
        story_ledger.is_echo_render('hash', http=http, strict=True)


def test_shared_queue_outage_cannot_become_empty_queue(monkeypatch):
    monkeypatch.setattr(story_sort_queue, '_supabase_conf', lambda: ('https://example.invalid', 'test'))
    http=SimpleNamespace(get=lambda *a,**k:SimpleNamespace(status_code=503))
    with pytest.raises(ValueError,match='unavailable'):
        story_sort_queue.pending('gym',http=http,strict=True)


def test_strict_discovery_propagates_to_both_classification_stores(monkeypatch):
    seen=[]
    monkeypatch.setattr(story_ledger,'is_echo_render',lambda value,**kw:seen.append(('ledger',kw)) or False)
    monkeypatch.setattr(story_sort_queue,'pending',lambda gym,**kw:seen.append(('queue',kw)) or [])
    assert not story_candidates._is_finished_render({'content_hash':'hash'},strict=True)
    assert story_candidates._pending_ambiguous_ids('gym',strict=True)==set()
    assert seen==[('ledger',{'strict':True}),('queue',{'strict':True})]
