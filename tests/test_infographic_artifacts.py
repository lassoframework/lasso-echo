from agent import infographic_artifacts as artifacts


def test_shared_record_retains_source_and_review_without_local_paths(monkeypatch):
    monkeypatch.setattr(artifacts.config, 'supabase_url', lambda: 'https://example.test')
    monkeypatch.setattr(artifacts.config, 'supabase_service_key', lambda: 'test-key')
    monkeypatch.setattr(artifacts, 'reviewed_asset', lambda path: {
        'image_sha256': 'a'*64, 'policy_version': artifacts.POLICY_VERSION,
        'response_id': 'generation', 'review_response_id': 'review',
        'path': '/temporary/image.png', 'prompt': 'verbose prompt'})
    calls = []
    class Http:
        def post(self, url, **kwargs):
            calls.append(kwargs)
            return type('Response', (), {'status_code': 201})()
    record = artifacts.ArtifactStore(Http()).save('lasso', 'https://image.test/card.png',
        '/temporary/image.png', {'source_id': 'brain', 'source_hash': 'b'*64})
    assert record['review_response_id'] == 'review'
    assert 'path' not in record and 'prompt' not in record
    assert calls[0]['json']['tenant'] == 'lasso'
    assert calls[0]['json']['source_identity']['source_hash'] == 'b'*64


def test_persistence_failure_is_not_reported_as_saved(monkeypatch):
    import pytest
    monkeypatch.setattr(artifacts.config, 'supabase_url', lambda: 'https://example.test')
    monkeypatch.setattr(artifacts.config, 'supabase_service_key', lambda: 'test-key')
    monkeypatch.setattr(artifacts, 'reviewed_asset', lambda path: {'image_sha256': 'a'*64})
    class Http:
        def post(self, *args, **kwargs):
            return type('Response', (), {'status_code': 500})()
    with pytest.raises(RuntimeError):
        artifacts.ArtifactStore(Http()).save('lasso', 'https://image.test/card.png',
            'image.png', {'source_id': 'brain', 'source_hash': 'b'*64})
