"""Small immutable reader projections of the already sanitized public snapshot."""
from __future__ import annotations
from collections import defaultdict
from copy import deepcopy
import hashlib
import re

from history_store import dump

READER_SCHEMA_VERSION = '1.0.0'
WINDOW_YEARS = 50
API_PATTERN = r'api/history/(?:version\.json|snapshots/[a-f0-9]{64}\.json|assets/AS-[a-f0-9]{64}\.(?:jpg|png|webp)|reader/[a-f0-9]{64}/(?:manifest\.json|catalog\.json|windows/-?\d+\.json|details/[a-f0-9]{2}\.json))'


def subset(data, entries, compact=False):
    ids = {e['id'] for e in entries}
    events = []
    for original in entries:
        entry = deepcopy(original)
        entry['relations'] = [r for r in entry['relations'] if r['target_event_id'] in ids]
        if compact:
            entry.update(body=[], citations=[], notes=[])
        events.append(entry)
    sources = {c['source_id'] for e in events for c in e['citations']}
    return {**data, 'events': events, 'sources': [s for s in data['sources'] if s['id'] in sources],
            'counts': {'total': len(events), 'unknown_date': sum(e['time']['start_year'] is None for e in events),
                       **{k: sum(e['entry_kind'] == k for e in events) for k in ('canonical', 'unlinked', 'context')}}}


def build_reader(data):
    """Keep the legacy snapshot intact; candidates never enter reader files.

    Compact entries retain the real identity/date/summary/image/search fields.
    Body, citations and source metadata arrive together when a detail is opened.
    Relations outside a subset remain in the complete catalogue and are preserved
    by the client when detail fields are merged.
    """
    if data.get('scope') != 'public_database' or not re.fullmatch('[a-f0-9]{64}', data.get('revision', '')):
        raise ValueError('Reader projection requires a sanitized public revision')
    prefix = 'api/history/reader/' + data['revision'] + '/'
    files = {}

    def put(name, value):
        raw = dump(value).encode()
        path = prefix + name + '.json'
        files[path] = raw
        return {'url': '/' + path, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}

    visible = [e for e in data['events'] if e['content_status'] != 'candidate']
    catalog = subset(data, visible, compact=True)
    windows, details = defaultdict(list), defaultdict(list)
    for entry in visible:
        time = entry['time']
        if time['start_year'] is not None:
            for start in range(time['start_year'] // WINDOW_YEARS * WINDOW_YEARS,
                               time['end_year'] // WINDOW_YEARS * WINDOW_YEARS + 1, WINDOW_YEARS):
                windows[start].append(entry)
        details[hashlib.sha256(entry['id'].encode()).hexdigest()[:2]].append(entry)
    manifest = {'schema_version': READER_SCHEMA_VERSION, 'revision': data['revision'], 'window_years': WINDOW_YEARS,
                'counts': catalog['counts'], 'time_bounds': catalog['time_bounds'], 'catalog': put('catalog', catalog),
                'windows': {str(k): put('windows/' + str(k), subset(data, entries, compact=True)) for k, entries in sorted(windows.items())},
                'details': {k: put('details/' + k, subset(data, entries)) for k, entries in sorted(details.items())}}
    reference = put('manifest', manifest)
    return files, reference, manifest


if __name__ == '__main__':
    import argparse
    import gzip
    import json
    from pathlib import Path
    from history_store import ROOT, validate_contract
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True, help='Already sanitized public snapshot (not a private database export)')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = args.snapshot.read_bytes()
    data = json.loads(raw)
    validate_contract(data, json.loads((ROOT / 'content/store/frontend.schema.json').read_text()))
    if data['scope'] != 'public_database':
        raise ValueError('Only a sanitized public snapshot can be projected')
    files, reader, manifest = build_reader(data)
    snapshot = 'api/history/snapshots/' + data['revision'] + '.json'
    files[snapshot] = raw
    version = {'schema_version': '1.0.0', 'revision': data['revision'], 'url': '/' + snapshot, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'reader': reader}
    files['api/history/version.json'] = dump(version).encode()
    for name, value in files.items():
        path = args.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    print(dump({'files': len(files), 'counts': manifest['counts'], 'catalog_bytes': manifest['catalog']['bytes'], 'catalog_gzip_bytes': len(gzip.compress(files[manifest['catalog']['url'][1:]], mtime=0))}))
