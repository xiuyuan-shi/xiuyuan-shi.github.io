#!/usr/bin/env python3
"""The single local history repository. No network access or automatic publication."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / 'content/store'
VERSION = '1.0.0'


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else dump(value).encode()).hexdigest()


def identity(prefix, *parts):
    return prefix + '-' + digest(list(parts))[:24]


def canonical_url(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise ValueError('Source must have a public HTTPS URL without credentials')
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and not (parts.hostname.endswith('bilibili.com') and k == 't')]
    return urlunsplit(('https', parts.netloc.lower(), parts.path or '/', urlencode(sorted(query)), ''))


def validate_contract(record, schema):
    """Validate the JSON Schema subset used by the local versioned contracts."""
    types = {'object': dict, 'array': list, 'string': str, 'integer': int,
             'number': (int, float), 'boolean': bool, 'null': type(None)}

    def check(value, rule, path='$'):
        if '$ref' in rule:
            target = schema
            for part in rule['$ref'][2:].split('/'):
                target = target[part]
            return check(value, target, path)
        if 'type' in rule:
            kinds = rule['type'] if isinstance(rule['type'], list) else [rule['type']]
            if not any(isinstance(value, types[t]) and not (t in ('integer', 'number') and isinstance(value, bool)) for t in kinds):
                raise ValueError(f'{path}: expected {kinds}')
        if 'enum' in rule and value not in rule['enum']:
            raise ValueError(f'{path}: unknown value {value!r}')
        if isinstance(value, dict):
            for key in rule.get('required', []):
                if key not in value:
                    raise ValueError(f'{path}: missing {key}')
            props = rule.get('properties', {})
            if rule.get('additionalProperties') is False and set(value) - set(props):
                raise ValueError(f'{path}: unknown fields {set(value) - set(props)}')
            for key, child in value.items():
                check(child, props.get(key, {}), f'{path}.{key}')
        if isinstance(value, list):
            if rule.get('uniqueItems') and len({dump(v) for v in value}) != len(value):
                raise ValueError(f'{path}: duplicates')
            for i, child in enumerate(value):
                check(child, rule.get('items', {}), f'{path}[{i}]')
        if isinstance(value, str) and len(value) < rule.get('minLength', 0):
            raise ValueError(f'{path}: empty string')
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            if not math.isfinite(value) or value < rule.get('minimum', -math.inf) or value > rule.get('maximum', math.inf):
                raise ValueError(f'{path}: out of bounds')

    check(record, schema)


def validate_record(record):
    """Validate the source record contract and its semantic constraints."""
    validate_contract(record, json.loads((CONTRACT / 'record.schema.json').read_text()))
    if not record['title'].strip():
        raise ValueError('A source record needs a readable title')
    time = record['time']
    start, end = time['start_year'], time['end_year']
    if start == 0 or end == 0 or (start is not None and end is not None and start > end):
        raise ValueError('Historical dates have no year zero; ranges must be ordered')
    if (start is None) != (end is None):
        raise ValueError('An open/uncertain date must remain unknown, not gain an invented bound')
    if start is None and time['kind'] != 'unknown':
        raise ValueError('Unknown years must use unknown time kind')
    if time['kind'] == 'point' and start != end:
        raise ValueError('Point dates cannot contain a multi-year range')
    if time['date_start']:
        match = re.fullmatch(r'(-?\d{1,6})-(\d{2})-(\d{2})', time['date_start'])
        if not match or int(match[1]) != start or not 1 <= int(match[2]) <= 12:
            raise ValueError('date_start must agree with the historical start year')
        year, month, day = map(int, match.groups())
        # Source calendar is retained; this only rejects impossible civil month lengths.
        limit = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
        if not 1 <= day <= limit:
            raise ValueError('Impossible calendar day')
    for evidence in record['evidence']:
        loc = evidence['locator']
        if evidence['status'] != 'link_only' and not loc:
            raise ValueError('Located evidence needs a locator')
        if 'start_seconds' in loc:
            if not 0 <= loc['start_seconds'] <= loc.get('end_seconds', loc['start_seconds']):
                raise ValueError('Invalid source time interval')
    for relation in record['relations']:
        if relation['type'] in ('causes', 'influences') and not relation['basis'].get('evidence'):
            raise ValueError('Causal/interpretive relations require their own evidence')
    for place in record['places']:
        coordinates = place['coordinates']
        if coordinates and not any(e['field'] == 'places.coordinates' and e['status'] == 'located'
                                   and e['locator'].get('locator') == coordinates['evidence_locator'] for e in record['evidence']):
            raise ValueError('Coordinates need their own located source evidence')


def blank_record(title, record_type='event'):
    return {'title': title, 'record_type': record_type, 'history_scope': 'unknown', 'aliases': [],
            'time': {'display': '', 'start_year': None, 'end_year': None, 'date_start': None,
                     'kind': 'unknown', 'precision': 'unknown', 'approximate': False,
                     'calendar': 'source_calendar_not_converted', 'basis': 'not_stated', 'original': None},
            'civilizations': [], 'categories': [], 'places': [], 'people': [], 'polities': [],
            'summary': None, 'body': [], 'status': 'candidate', 'media': [], 'evidence': [],
            'relations': [], 'selection': {}, 'notes': []}


class Store:
    def __init__(self, root=ROOT, directory=None):
        self.root = Path(root).resolve()
        self.directory = Path(directory or self.root / 'content/store').resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'history.sqlite3', timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript((CONTRACT / 'schema.sql').read_text())
        version = self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if version and version[0] != VERSION:
            raise ValueError(f'Unsupported history store version {version[0]}')
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version',?)", (VERSION,))
        self.db.commit()

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self
            from history_identity import invalidate_changed_reviews
            invalidate_changed_reviews(self)
            errors = self.validate()
            errors.extend(self.audit_evidence()['mismatches'])
            if errors:
                raise ValueError('; '.join(errors[:10]))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def asset(self, path, expected=None, data=None):
        path = Path(path)
        absolute = (self.root / path).resolve()
        if not absolute.is_relative_to(self.root):
            raise ValueError(f'Asset escapes project: {path}')
        relative = absolute.relative_to(self.root).as_posix()
        if data is None and absolute.is_file():
            data = absolute.read_bytes()
        if expected and not re.fullmatch('[a-f0-9]{64}', expected):
            raise ValueError(f'Invalid SHA256 for {path}')
        sha = digest(data) if data is not None else expected
        if not sha:
            raise FileNotFoundError(f'Cannot register an unhashed missing asset: {path}')
        if expected and sha != expected:
            raise ValueError(f'Asset hash mismatch: {path}')
        asset_id = 'AS-' + sha
        old = self.db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
        suffix = path.suffix.lower() if path.suffix.lower() in ('.json', '.jsonl', '.png', '.jpg', '.jpeg', '.webp', '.html', '.pdf', '.txt') else '.bin'
        blob_path = old['path'] if old else f'blobs/{sha[:2]}/{sha}{suffix}'
        target = self.directory / blob_path
        if target.exists() and digest(target.read_bytes()) != sha:
            raise ValueError(f'Corrupted canonical blob: {blob_path}')
        if data is not None and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                stream.write(data)
                temp = stream.name
            os.replace(temp, target)
        present = target.is_file()
        self.db.execute('''INSERT INTO assets VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,bytes=excluded.bytes
                        WHERE assets.state!=excluded.state OR assets.bytes IS NOT excluded.bytes''',
                        (asset_id, sha, blob_path, target.stat().st_size if present else None, 'present' if present else 'missing'))
        self.db.execute('INSERT OR IGNORE INTO asset_origins VALUES (?,?)', (asset_id, relative))
        return asset_id

    def snapshot(self, path, expected=None):
        path = Path(path)
        absolute = (self.root / path).resolve()
        if not absolute.is_relative_to(self.root):
            raise ValueError('Input snapshot escapes project')
        if absolute.is_file():
            data = absolute.read_bytes()
        elif expected and re.fullmatch('[a-f0-9]{64}', expected):
            archived = self.db.execute('SELECT path FROM assets WHERE sha256=? AND state=?', (expected, 'present')).fetchone()
            if not archived:
                raise FileNotFoundError(f'Missing input and archived snapshot: {path}')
            archived_path = (self.directory / archived['path']).resolve()
            if not archived_path.is_relative_to(self.directory):
                raise ValueError('Archived input escapes store')
            data = archived_path.read_bytes()
        else:
            raise FileNotFoundError(f'Required input missing: {path}')
        sha = digest(data)
        if expected and sha != expected:
            raise ValueError(f'Input snapshot hash mismatch: {path}')
        asset_id = self.asset(path, sha, data)
        snapshot_id = identity('IN', path.as_posix(), sha)
        self.db.execute('INSERT OR IGNORE INTO input_snapshots VALUES (?,?,?,?)', (snapshot_id, path.as_posix(), sha, asset_id))
        return json.loads(data), snapshot_id, sha

    def source(self, url, kind='web', **metadata):
        url = canonical_url(url)
        source_id = identity('SRC', url)
        self.db.execute('INSERT OR IGNORE INTO sources VALUES (?,?,?)', (source_id, url, kind))
        version_id = identity('SV', source_id, metadata)
        self.db.execute('INSERT OR IGNORE INTO source_versions VALUES (?,?,?)', (version_id, source_id, dump(metadata)))
        return version_id

    def event(self, namespace, external_id):
        old = self.db.execute('SELECT event_id FROM event_aliases WHERE namespace=? AND external_id=?', (namespace, external_id)).fetchone()
        if old:
            return old[0]
        event_id = identity('EV', namespace, external_id)
        self.db.execute('INSERT INTO events(id) VALUES (?)', (event_id,))
        self.db.execute('INSERT INTO event_aliases VALUES (?,?,?)', (namespace, external_id, event_id))
        return event_id

    def observe(self, namespace, external_id, family, payload, original, snapshot, source_version,
                event_id=None, prefer=False):
        validate_record(payload)
        record_id = identity('REC', namespace, external_id)
        revision = identity('REV', record_id, source_version, payload, original)
        previous = self.db.execute('SELECT * FROM records WHERE id=?', (record_id,)).fetchone()
        linked = previous['event_id'] if previous and previous['event_id'] else event_id
        if previous and previous['event_id'] and event_id and previous['event_id'] != event_id:
            raise ValueError('Import cannot change a reviewed identity link')
        self.db.execute('''INSERT INTO records VALUES (?,?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET current_revision=excluded.current_revision,active=1,event_id=excluded.event_id
                        WHERE records.current_revision!=excluded.current_revision OR records.active!=1 OR records.event_id IS NOT excluded.event_id''',
                        (record_id, namespace, external_id, family, linked, revision))
        self.db.execute('INSERT OR IGNORE INTO record_revisions VALUES (?,?,?,?,?,?)',
                        (revision, record_id, snapshot, source_version, dump(payload), dump(original)))
        for index, row in enumerate(payload['evidence']):
            self.db.execute('INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?)',
                            (identity('EVD', revision, index), revision, row['source_version_id'], row['field'], dump(row['locator']), row['asset_id']))
        for row in payload['media']:
            self.db.execute('INSERT OR IGNORE INTO record_media VALUES (?,?,?)', (revision, row['asset_id'], dump(row)))
        for index, row in enumerate(payload['relations']):
            self.db.execute('INSERT OR IGNORE INTO relations VALUES (?,?,?,?,?)',
                            (identity('REL', revision, index), revision, row['target_event_id'], row['type'], dump(row['basis'])))
        if linked:
            event = self.db.execute('SELECT * FROM events WHERE id=?', (linked,)).fetchone()
            if not event:
                raise ValueError('Unknown canonical event')
            if previous and previous['current_revision'] != revision:
                self.db.execute("UPDATE events SET review_status='pending' WHERE id=?", (linked,))
            if ((prefer and not event['selection_locked']) or not event['preferred_record_id']) and event['preferred_record_id'] != record_id:
                self.db.execute('UPDATE events SET preferred_record_id=? WHERE id=?', (record_id, linked))
        return record_id

    def link(self, record_id, event_id, reason, review_id=None):
        if not reason.strip():
            raise ValueError('A reconciliation decision needs a reason')
        record = self.db.execute('SELECT * FROM records WHERE id=?', (record_id,)).fetchone()
        event = self.db.execute('SELECT * FROM events WHERE id=? AND redirect_to IS NULL', (event_id,)).fetchone()
        if not record or not event:
            raise ValueError('Unknown record or canonical target')
        if record['event_id'] and record['event_id'] != event_id:
            raise ValueError('Use merge for two existing events; link never silently moves a record')
        if record['event_id'] == event_id:
            return
        if review_id is None:
            self.db.execute('DELETE FROM record_identity_state WHERE record_id=?', (record_id,))
        self.db.execute('UPDATE records SET event_id=? WHERE id=?', (event_id, record_id))
        self.db.execute("UPDATE events SET review_status='pending' WHERE id=?", (event_id,))
        self.db.execute("INSERT INTO decisions(action,subject_id,target_id,reason) VALUES ('link',?,?,?)", (record_id, event_id, reason))

    def merge(self, winner, loser, reason):
        if not reason.strip() or winner == loser:
            raise ValueError('Distinct events and a reason are required')
        if self.db.execute('SELECT COUNT(*) FROM events WHERE id IN (?,?) AND redirect_to IS NULL', (winner, loser)).fetchone()[0] != 2:
            raise ValueError('Merge requires two live canonical events')
        self.db.execute('UPDATE records SET event_id=? WHERE event_id=?', (winner, loser))
        self.db.execute('UPDATE event_aliases SET event_id=? WHERE event_id=?', (winner, loser))
        self.db.execute("UPDATE events SET redirect_to=?,preferred_record_id=NULL,review_status='pending' WHERE id=?", (winner, loser))
        self.db.execute("UPDATE events SET review_status='pending',selection_locked=1 WHERE id=?", (winner,))
        self.db.execute("INSERT INTO decisions(action,subject_id,target_id,reason) VALUES ('merge',?,?,?)", (loser, winner, reason))

    def validate(self, files=False):
        errors = []
        errors.extend(str(tuple(row)) for row in self.db.execute('PRAGMA foreign_key_check'))
        for row in self.db.execute('''SELECT s.record_id FROM record_identity_state s JOIN identity_reviews i ON i.id=s.review_id
                                     WHERE s.record_id!=i.record_id'''):
            errors.append(f'Identity review belongs to another record: {row[0]}')
        for row in self.db.execute('''SELECT i.id FROM identity_reviews i JOIN record_revisions v ON v.id=i.record_revision_id
                                     WHERE i.record_id!=v.record_id'''):
            errors.append(f'Identity review points to another record revision: {row[0]}')
        for row in self.db.execute('''SELECT t.review_id FROM identity_review_targets t
                                     JOIN record_revisions v ON v.id=t.preferred_revision_id WHERE t.preferred_record_id!=v.record_id'''):
            errors.append(f'Identity candidate revision has wrong owner: {row[0]}')
        if self.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            errors.append('SQLite integrity check failed')
        for row in self.db.execute('SELECT r.*,v.record_id AS owner FROM records r LEFT JOIN record_revisions v ON v.id=r.current_revision'):
            if row['owner'] != row['id']:
                errors.append(f"Missing/wrong current revision: {row['id']}")
        for row in self.db.execute('SELECT e.*,r.event_id,r.active FROM events e LEFT JOIN records r ON r.id=e.preferred_record_id WHERE e.redirect_to IS NULL'):
            if not row['preferred_record_id'] or row['event_id'] != row['id'] or not row['active']:
                errors.append(f"Canonical selection missing or inactive: {row['id']}")
        for row in self.db.execute('SELECT id,normalized_json FROM record_revisions'):
            try:
                validate_record(json.loads(row['normalized_json']))
            except ValueError as error:
                errors.append(f"{row['id']}: {error}")
        if files:
            for row in self.db.execute('SELECT * FROM assets'):
                path = (self.directory / row['path']).resolve()
                if not path.is_relative_to(self.directory) or not path.is_file():
                    errors.append(f"Missing canonical asset: {row['id']}")
                elif path.stat().st_size != row['bytes'] or digest(path.read_bytes()) != row['sha256']:
                    errors.append(f"Invalid canonical asset: {row['id']}")
            errors.extend(self.audit_evidence()['mismatches'])
        return errors

    def audit_evidence(self):
        result = {'checked_blocks': 0, 'missing_block_snapshots': 0, 'mismatches': [], 'video_frame_locators': 0}
        snapshots = {}
        versions = {}
        query = '''SELECT e.*,v.metadata_json FROM evidence e
                   JOIN records r ON r.current_revision=e.revision_id AND r.active=1
                   JOIN source_versions v ON v.id=e.source_version_id'''
        for row in self.db.execute(query):
            locator = json.loads(row['locator_json'])
            if 'start_seconds' in locator and row['asset_id']:
                result['video_frame_locators'] += 1
            expected = locator.get('block_sha256')
            if not expected:
                continue
            version = row['source_version_id']
            if version not in versions:
                versions[version] = json.loads(row['metadata_json'])
            meta = versions[version]
            snapshot = meta.get('block_snapshot')
            if not snapshot:
                result['missing_block_snapshots'] += 1
                continue
            if snapshot not in snapshots:
                asset = self.db.execute('SELECT a.path FROM input_snapshots i JOIN assets a ON a.id=i.asset_id WHERE i.id=?', (snapshot,)).fetchone()
                path = self.directory / asset[0] if asset else None
                snapshots[snapshot] = json.loads(path.read_text()) if path and path.is_file() else {}
            source = snapshots[snapshot].get(meta.get('block_snapshot_url'), {})
            blocks = source.get('blocks', [])
            hashes = [digest(block.encode()) for block in blocks]
            number = re.fullmatch(r'可读正文块 (\d+)', locator.get('locator', ''))
            actual = hashes[int(number[1]) - 1:int(number[1])] if number else hashes
            if expected not in actual:
                result['mismatches'].append(f"Evidence block hash/locator mismatch: {row['id']}")
            else:
                result['checked_blocks'] += 1
        return result

    def summary(self):
        counts = {table: self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                  for table in ('events', 'records', 'record_revisions', 'sources', 'source_versions', 'evidence', 'assets', 'input_snapshots', 'ingest_runs')}
        counts['canonical_events'] = self.db.execute('SELECT COUNT(*) FROM events WHERE redirect_to IS NULL').fetchone()[0]
        counts['unlinked_records'] = self.db.execute('SELECT COUNT(*) FROM records WHERE event_id IS NULL AND active=1').fetchone()[0]
        counts['missing_assets'] = self.db.execute("SELECT COUNT(*) FROM assets WHERE state='missing'").fetchone()[0]
        counts['published_events'] = self.db.execute("SELECT COUNT(*) FROM events WHERE review_status='published' AND redirect_to IS NULL").fetchone()[0]
        counts['families'] = dict(self.db.execute('SELECT family,COUNT(*) FROM records WHERE active=1 GROUP BY family'))
        from collections import Counter
        from history_identity import current_reviews
        counts['identity_review_counts'] = dict(Counter(row['status'] for row in current_reviews(self)))
        return counts

    def projections(self, published=False):
        output = []
        for event in self.db.execute('SELECT * FROM events WHERE redirect_to IS NULL ORDER BY id'):
            if published and event['review_status'] != 'published':
                continue
            row = self.db.execute('SELECT v.* FROM records r JOIN record_revisions v ON v.id=r.current_revision WHERE r.id=?', (event['preferred_record_id'],)).fetchone()
            value = json.loads(row['normalized_json'])
            records = list(self.db.execute('SELECT r.id,r.current_revision,r.family FROM records r WHERE event_id=? AND active=1 ORDER BY id', (event['id'],)))
            conflicts = []
            for other in records:
                alternative = json.loads(self.db.execute('SELECT normalized_json FROM record_revisions WHERE id=?', (other['current_revision'],)).fetchone()[0])
                if other['family'] in ('catalog', 'web_detail'):
                    value['aliases'] = list(dict.fromkeys([*value['aliases'], *alternative['aliases'],
                        *([alternative['title']] if alternative['title'] != value['title'] else [])]))
                for field in ('title', 'time', 'places', 'record_type', 'summary'):
                    if alternative[field] != value[field]:
                        conflicts.append({'record_id': other['id'], 'field': field, 'value': alternative[field], 'status': 'retained_alternative'})
            related = []
            for rel in value['relations']:
                target = rel['target_event_id']
                seen = set()
                while target not in seen:
                    seen.add(target)
                    redirect = self.db.execute('SELECT redirect_to FROM events WHERE id=?', (target,)).fetchone()[0]
                    if not redirect:
                        break
                    target = redirect
                if target != event['id']:
                    related.append({**rel, 'target_event_id': target})
            output.append({'id': event['id'], 'review_status': event['review_status'],
                           'preferred_record_id': event['preferred_record_id'], 'record_ids': [r['id'] for r in records],
                           **value, 'relations': related, 'alternatives': conflicts,
                           'map_status': 'coordinates_available' if any(p['coordinates'] for p in value['places']) else 'location_pending',
                           'missing_fields': [name for name, missing in (
                               ('summary', not value['summary']), ('body', not value['body']),
                               ('categories', not value['categories']), ('places', not value['places']),
                               ('coordinates', not any(p['coordinates'] for p in value['places'])),
                               ('illustration', not any(m['role'] == 'illustration' for m in value['media']))
                           ) if missing]})
        return output

    def reconciliation(self):
        # Suggestions only. A paragraph can contain several events; never auto-link it.
        def norm(text):
            return re.sub(r'[^\w\u4e00-\u9fff]', '', unicodedata.normalize('NFKC', text).lower())
        names = []
        for event in self.projections():
            for name in [event['title'], *event['aliases']]:
                if len(norm(name)) >= 4:
                    names.append((norm(name), event['id'], event['time']))
        from history_identity import current_reviews
        reviewed = {r['record_id']: r for r in current_reviews(self)}
        result = []
        for row in self.db.execute('SELECT r.*,v.normalized_json FROM records r JOIN record_revisions v ON v.id=r.current_revision WHERE r.event_id IS NULL AND r.active=1 ORDER BY r.id'):
            value = json.loads(row['normalized_json'])
            if value['record_type'] in ('period_label', 'war_context', 'narration_topic', 'unparsed'):
                continue
            title = norm(value['title'])
            matches = {}
            for name, event_id, time in names:
                if name in title or (len(title) >= 6 and title in name):
                    matches[event_id] = {'event_id': event_id, 'reason': '名称或别名包含匹配，须人工核对事件范围',
                                         'candidate_time': time, 'date_disagrees': value['time']['start_year'] is not None and time['start_year'] is not None and value['time']['start_year'] != time['start_year']}
            assessment = reviewed.get(row['id'])
            if assessment:
                matches = {t['event_id']: {'event_id': t['resolved_event_id'], 'relationship': t['relationship'],
                    'comparison': t['comparison'], 'reason': assessment['explanation']} for t in assessment['targets']}
            result.append({'record_id': row['id'], 'namespace': row['namespace'], 'external_id': row['external_id'],
                           'title': value['title'], 'time': value['time'], 'suggestions': list(matches.values()),
                           'status': assessment['status'] if assessment else 'unresolved', 'identity_review': assessment})
        return result

    def export(self, path, published=False):
        # All files are reproducible read models, never a second write source.
        path = Path(path).resolve()
        self.db.execute('BEGIN')
        try:
            events = self.projections(published)
            assets = [dict(r) for r in self.db.execute('SELECT * FROM assets ORDER BY id')]
            sources = [dict(r) for r in self.db.execute('SELECT * FROM sources ORDER BY id')]
            versions = [{key: value for key, value in dict(r).items() if key != 'metadata_json'} |
                        {'metadata': json.loads(r['metadata_json'])} for r in self.db.execute('SELECT * FROM source_versions ORDER BY id')]
            records = [{**{key: row[key] for key in ('id', 'namespace', 'external_id', 'family', 'event_id', 'source_version_id')},
                        'revision_id': row['current_revision'], 'record': json.loads(row['normalized_json'])}
                       for row in self.db.execute('''SELECT r.*,v.source_version_id,v.normalized_json FROM records r
                           JOIN record_revisions v ON v.id=r.current_revision WHERE r.active=1 ORDER BY r.id''')]
            from history_identity import current_reviews, report as identity_report
            assessments = [] if published else current_reviews(self)
            assessment_map = {r['record_id']: r for r in assessments}
            if not published:
                for record in records:
                    record['identity_review'] = assessment_map.get(record['id'])
            if published:
                # A publish view must not carry unpublished records, source metadata or review queues.
                asset_ids = {m['asset_id'] for event in events for m in event['media']}
                asset_ids.update(e['asset_id'] for event in events for e in event['evidence'] if e['asset_id'])
                version_ids = {e['source_version_id'] for event in events for e in event['evidence']}
                version_ids.update(m['provenance']['source_version_id'] for event in events for m in event['media'] if m['provenance'].get('source_version_id'))
                assets = [a for a in assets if a['id'] in asset_ids]
                versions = [v for v in versions if v['id'] in version_ids]
                source_ids = {v['source_id'] for v in versions}
                sources = [s for s in sources if s['id'] in source_ids]
                records = []
                for event in events:
                    event.pop('alternatives')
                    event.pop('record_ids')
            payload = {'schema_version': VERSION, 'scope': 'published' if published else 'review',
                       'asset_base': os.path.relpath(self.directory, path).replace(os.sep, '/'),
                       'events': events, 'records': records,
                       'summary': {'published_events': len(events)} if published else self.summary(),
                       'assets': assets, 'sources': sources, 'source_versions': versions,
                       'identity_review_report': None if published else identity_report(self)}
            queue = [] if published else self.reconciliation()
        finally:
            self.db.rollback()
        path.mkdir(parents=True, exist_ok=True)
        for name, value in [('history.json', payload), ('reconciliation.json', queue), ('identity-reviews.json', assessments)]:
            temp = path / (name + '.tmp')
            temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
            os.replace(temp, path / name)
        with (path / 'reconciliation.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['record_id', 'source_namespace', 'external_id', 'title', 'suggested_event_ids', 'status'])
            for row in queue:
                # Neutralize spreadsheet formulas in external text.
                writer.writerow([("'" + str(v)) if str(v).startswith(('=', '+', '-', '@')) else v for v in
                                 [row['record_id'], row['namespace'], row['external_id'], row['title'],
                                  ','.join(s['event_id'] for s in row['suggestions']), row['status']]])
        with (path / 'identity-reviews.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['来源记录ID', '来源命名空间', '原ID', '原文事件', '原文纪年', '核对读到的年份', '年份依据',
                             '记录范围', '核对状态', '候选事件', '候选纪年', '候选ID', '原因代码', '核对说明', '视频定位', '目检原帧', '来源修订ID'])
            for row in assessments:
                basis = row['basis']
                reading = basis.get('source_time_reading', {})
                urls = list(dict.fromkeys(e['locator']['source_url'] for e in basis.get('source_evidence', []) if e['locator'].get('source_url')))
                values = [row['record_id'], row['namespace'], row['external_id'], row['title'], row['source_time']['display'],
                          reading.get('start_year'), reading.get('basis', ''), basis.get('source_observation_scope', ''), row['status'],
                          '；'.join(t['comparison'].get('candidate_title', '') for t in row['targets']),
                          '；'.join(t['comparison'].get('candidate_time', {}).get('display', '') for t in row['targets']),
                          ','.join(t['resolved_event_id'] for t in row['targets']), ','.join(row['reason_codes']), row['explanation'],
                          '；'.join(urls), basis.get('source_frame', ''), row['record_revision_id']]
                writer.writerow([("'" + v) if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for v in values])
        return {'events': len(events), 'unresolved_event_records': len(queue), 'path': str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('sync', help='Import every registered collector; local only')
    validate = sub.add_parser('validate')
    validate.add_argument('--files', action='store_true')
    validate.add_argument('--evidence', action='store_true', help='Require archived body blocks for every current field locator')
    sub.add_parser('status')
    export = sub.add_parser('export')
    export.add_argument('--output', type=Path)
    export.add_argument('--published', action='store_true')
    link = sub.add_parser('link')
    link.add_argument('record_id'); link.add_argument('event_id'); link.add_argument('--reason', required=True)
    merge = sub.add_parser('merge')
    merge.add_argument('winner'); merge.add_argument('loser'); merge.add_argument('--reason', required=True)
    args = parser.parse_args()
    store = Store(args.root)
    try:
        if args.action == 'sync':
            from history_ingest import sync
            result = sync(store)
        elif args.action == 'validate':
            errors = store.validate(args.files)
            audit = store.audit_evidence()
            if args.evidence:
                errors.extend(audit['mismatches'])
                if audit['missing_block_snapshots']:
                    errors.append(f"Missing body-block snapshots: {audit['missing_block_snapshots']}")
            result = {'valid': not errors, 'errors': sorted(set(errors)), 'evidence_audit': audit, 'summary': store.summary()}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if errors:
                raise SystemExit(1)
            return
        elif args.action == 'status':
            result = store.summary()
        elif args.action == 'export':
            result = store.export(args.output or store.directory / 'exports', args.published)
        else:
            with store.transaction():
                if args.action == 'link':
                    store.link(args.record_id, args.event_id, args.reason)
                else:
                    store.merge(args.winner, args.loser, args.reason)
            result = store.summary()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
