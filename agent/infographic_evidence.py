"""Verify a reusable graphic against its exact bytes and current review contract."""
import hashlib
import json
from pathlib import Path

POLICY_VERSION = 'lasso-astra-2026-09-14-punctuation-v1'


def brain_snapshot():
    from . import config
    root = Path(__file__).resolve().parent.parent
    source = Path(config.SOURCE_DOC_PATH)
    if not source.is_absolute():
        source = root / source
    knowledge = Path(config.KNOWLEDGE_DIR)
    if not knowledge.is_absolute():
        knowledge = root / knowledge
    paths = [source, *sorted(knowledge.glob('**/*.md'))]
    return {str(path.relative_to(root)) if path.is_relative_to(root) else str(path):
            hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.is_file()}


def reviewed_asset(path):
    try:
        image = Path(path)
        evidence = json.loads(Path(str(image) + '.review.json').read_text())
        copy = evidence['infographic_copy']
        texts = [copy['headline'], *copy['facts'], copy.get('cta', ''), copy.get('footer', '')]
        if not all(isinstance(text, str) for text in texts):
            return None
        if any(';' in text or ':' in text for text in texts):
            return None
        if not (evidence.get('policy_version') == POLICY_VERSION
                and evidence.get('brain_snapshot') and evidence['brain_snapshot'] == brain_snapshot()
                and evidence.get('brief_model') == 'gpt-6-astra'
                and evidence.get('grade_status') == 'PASS'
                and evidence.get('response_id') and evidence.get('review_response_id')
                and evidence.get('image_sha256') == hashlib.sha256(image.read_bytes()).hexdigest()):
            return None
        return evidence
    except (OSError, ValueError, KeyError, TypeError):
        return None
