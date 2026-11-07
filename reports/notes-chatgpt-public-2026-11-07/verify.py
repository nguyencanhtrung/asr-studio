"""Verify frozen public Notes evidence; does not call a provider or certify semantics."""
import hashlib
import json
from pathlib import Path

from asr_studio.notes_document import validate_document

root = Path(__file__).resolve().parent
plan = json.loads((root / 'plan.json').read_text())
summary = json.loads((root / 'generation-summary.json').read_text())
for name, digest in plan['input_sha256'].items():
    assert hashlib.sha256((root / 'input' / name).read_bytes()).hexdigest() == digest, name
rows = []
for case in plan['cases']:
    path = root / 'results' / (case['case'] + '.json')
    result = json.loads(path.read_text())
    snapshot = json.loads((root / 'input' / case['input']).read_text())
    assert result['status'] == 'succeeded', case['case']
    assert result['source_sha256'] == plan['input_sha256'][case['input']]
    for field in ['provider', 'model', 'output_language', 'prompt_version']:
        assert result[field] == plan[field], (case['case'], field)
    assert result['effort'] == plan['reasoning_effort']
    validate_document(result['document'], snapshot)
    refs = [sid for items in result['document'].values() if isinstance(items, list)
            for item in items for sid in item['source_ids']]
    ids = {segment['id'] for segment in snapshot['segments']}
    rows.append({
        'case': case['case'], 'status': result['status'], 'schema_valid': True,
        'source_segments': len(snapshot['segments']),
        'bullets': sum(len(items) for items in result['document'].values() if isinstance(items, list)),
        'citation_refs': len(refs), 'distinct_cited_ids': len(set(refs)),
        'unresolved_ids': len(set(refs) - ids),
        'provider_calls': result['coverage']['provider_calls'],
        'output_recoveries': result['coverage']['output_recoveries'],
        'elapsed_seconds': result['elapsed_seconds'],
        'result_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    })
actual = {
    'cases': rows,
    'total_provider_calls': sum(row['provider_calls'] for row in rows),
    'total_recoveries': sum(row['output_recoveries'] for row in rows),
    'generation_seconds': round(sum(row['elapsed_seconds'] for row in rows), 3),
    'total_bullets': sum(row['bullets'] for row in rows),
    'unresolved_ids': sum(row['unresolved_ids'] for row in rows),
}
assert actual == summary, 'Generation summary differs from raw evidence'
if (root / 'artifact-sha256.json').exists():
    for name, digest in json.loads((root / 'artifact-sha256.json').read_text()).items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
print(json.dumps({'status': 'pass', **{k: v for k, v in actual.items() if k != 'cases'}}))
