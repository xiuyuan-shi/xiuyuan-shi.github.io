#!/usr/bin/env python3
"""Read-only, live HTTP view of the canonical local history database."""
from __future__ import annotations

import argparse
from collections import defaultdict
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
import sqlite3
import threading
from urllib.parse import unquote, urlsplit

from history_store import ROOT, VERSION, dump

CONTEXT_TYPES = {'period_label', 'war_context', 'narration_topic', 'unparsed'}


def public_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None


def connect(directory):
    # Do not create a database, migrate a schema, or change journal settings here.
    db = sqlite3.connect((Path(directory).resolve() / 'history.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if not version or version[0] != VERSION:
        db.close()
        raise ValueError('Unsupported history database schema')
    return db


def image_type(data):
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def project(db):
    """One transaction, current active revisions only; never consult a trial list."""
    rows = list(db.execute('''SELECT r.*,v.source_version_id,v.normalized_json FROM records r
        JOIN record_revisions v ON v.id=r.current_revision WHERE r.active=1 ORDER BY r.id'''))
    records = {r['id']: (r, json.loads(r['normalized_json'])) for r in rows}
    linked = defaultdict(list)
    for row, value in records.values():
        if row['event_id']:
            linked[row['event_id']].append((row, value))
    identities = {r['id']: r for r in db.execute('SELECT * FROM events')}
    assets = {r['id']: r for r in db.execute('SELECT * FROM assets')}
    versions = {r['id']: r for r in db.execute('''SELECT v.*,s.canonical_url FROM source_versions v
        JOIN sources s ON s.id=v.source_id''')}
    sources = {}
    map_references = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_map_references'").fetchone():
        map_references = {(r['event_id'], r['record_id'], r['revision_id']): json.loads(r['reference_json'])
                          for r in db.execute('SELECT * FROM event_map_references')}

    def source(version_id):
        if version_id in sources:
            return
        row = versions[version_id]
        metadata = json.loads(row['metadata_json'])
        original = metadata.get('original') or {}
        attribution = metadata.get('attribution') if isinstance(metadata.get('attribution'), dict) else {}
        sources[version_id] = {
            'id': version_id, 'title': str(metadata.get('title') or original.get('title') or row['canonical_url']),
            'publisher': str(metadata.get('publisher') or ''),
            'author': str(metadata.get('author') or attribution.get('author') or (original.get('metadata') or {}).get('author') or ''),
            'url': public_url(row['canonical_url']),
            'read_at': original.get('fetched_at') or metadata.get('fetched_at'),
            'access_status': str(metadata.get('access_status') or 'not_stated'),
            'license': str(metadata['license']) if metadata.get('license') else None,
            'license_url': public_url(attribution.get('license_url') or metadata.get('license_url')),
            'attribution': str(attribution.get('modifications') or ''),
        }

    def resolved(event_id):
        seen = set()
        while event_id in identities and event_id not in seen:
            seen.add(event_id)
            target = identities[event_id]['redirect_to']
            if not target:
                return event_id
            event_id = target
        return None

    def entry(row, value, event=None):
        aliases = list(value['aliases'])
        if event:
            for other, alternative in linked[event['id']]:
                if other['family'] in ('catalog', 'web_detail'):
                    aliases.extend([alternative['title'], *alternative['aliases']])
        citations = []
        for evidence in value['evidence']:
            source(evidence['source_version_id'])
            locator = evidence['locator']
            citations.append({
                'source_id': evidence['source_version_id'], 'field': evidence['field'],
                'locator': locator.get('locator') or dump(locator),
                'block_sha256': locator.get('block_sha256'), 'status': evidence['status'],
            })
        if not any(c['source_id'] == row['source_version_id'] for c in citations):
            source(row['source_version_id'])
            citations.append({'source_id': row['source_version_id'], 'field': 'source_record',
                              'locator': '来源记录；字段支持范围见原始来源', 'block_sha256': None,
                              'status': 'link_only'})
        images = []
        for media in value['media']:
            if media['role'] not in ('illustration', 'evidence_frame'):
                continue
            asset = assets.get(media['asset_id'])
            if not asset or asset['state'] != 'present':
                continue
            rights, provenance = media['rights'], media['provenance']
            audit = rights.get('rights_audit') or {}
            images.append({
                'src': '/api/history/assets/' + asset['id'], 'sha256': asset['sha256'],
                'caption': media['caption'], 'depiction_type': media['depiction_type'],
                'creator': str(rights.get('creator') or rights.get('author') or ''),
                'credit': str(rights.get('credit') or ''), 'license': str(rights.get('license') or rights.get('status') or '许可待核'),
                'license_url': public_url(rights.get('license_url')),
                'source_url': public_url(provenance.get('description_url') or versions[row['source_version_id']]['canonical_url']),
                'rights_revision_url': public_url(audit.get('revision_url')),
                'obligations': audit.get('obligations') or [],
            })
        places = []
        for place in value['places']:
            coordinates = place['coordinates']
            if coordinates and not any(e['field'] == 'places.coordinates' and e['status'] == 'located'
                    and e['locator'].get('locator') == coordinates['evidence_locator'] for e in value['evidence']):
                coordinates = None
            places.append({k: place[k] for k in ('label', 'precision', 'role')} | {'coordinates': coordinates})
        relations = [{'target_event_id': resolved(r['target_event_id']), 'type': r['type']} for r in value['relations']]
        return {
            'id': event['id'] if event else row['id'], 'record_id': row['id'], 'revision_id': row['current_revision'],
            'entry_kind': 'canonical' if event else 'context' if value['record_type'] in CONTEXT_TYPES else 'unlinked',
            'review_status': event['review_status'] if event else 'pending', 'content_status': value['status'],
            'content_notice': None,
            **{k: value[k] for k in ('title', 'summary', 'body', 'civilizations', 'categories', 'history_scope', 'record_type', 'notes')},
            'aliases': list(dict.fromkeys(a for a in aliases if a != value['title'])),
            'time': {k: value['time'][k] for k in ('start_year', 'end_year', 'kind', 'precision', 'approximate', 'display')},
            'places': places, 'people': [p['name'] for p in value['people']], 'polities': [p['name'] for p in value['polities']],
            'images': images, 'citations': citations, 'relations': relations,
            'map_reference': map_references.get((event['id'], row['id'], row['current_revision'])) if event else None,
        }

    visible = lambda value: value['selection'].get('visibility') != 'hidden'
    events = [entry(*records[e['preferred_record_id']], event=e) for e in identities.values()
              if not e['redirect_to'] and e['preferred_record_id'] in records and visible(records[e['preferred_record_id']][1])]
    events.extend(entry(row, value) for row, value in records.values() if row['event_id'] is None and visible(value))
    ids = {e['id'] for e in events}
    for e in events:
        e['relations'] = [r for r in e['relations'] if r['target_event_id'] in ids and r['target_event_id'] != e['id']]
    events.sort(key=lambda e: (e['time']['start_year'] is None, e['time']['start_year'] or 0, e['title'], e['id']))
    years = [v for e in events for v in (e['time']['start_year'], e['time']['end_year']) if v is not None]
    payload = {
        'schema_version': VERSION, 'scope': 'local_database',
        'counts': {**{kind: sum(e['entry_kind'] == kind for e in events) for kind in ('canonical', 'unlinked', 'context')},
                   'total': len(events), 'unknown_date': sum(e['time']['start_year'] is None for e in events)},
        'time_bounds': {'min_year': min([-3500, *years]), 'max_year': max([2026, *years])},
        'events': events, 'sources': sorted(sources.values(), key=lambda s: s['id']),
    }
    payload['revision'] = hashlib.sha256(dump(payload).encode()).hexdigest()
    return payload


class Repository:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.lock = threading.Lock()
        self.cached = None
        self.fingerprint = None

    def snapshot(self):
        with self.lock:
            fingerprint = []
            for suffix in ('', '-wal', '-journal'):
                path = self.directory / ('history.sqlite3' + suffix)
                stat = path.stat() if path.exists() else None
                fingerprint.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) if stat else None)
            if self.cached is not None and fingerprint == self.fingerprint:
                return self.cached
            db = connect(self.directory)
            try:
                db.execute('BEGIN')
                payload = project(db)
            finally:
                db.close()
            self.cached = (dump(payload).encode(), '"' + payload['revision'] + '"')
            self.fingerprint = fingerprint
            return self.cached

    def image(self, asset_id):
        if not re.fullmatch(r'AS-[a-f0-9]{64}', asset_id):
            raise FileNotFoundError('Unknown image')
        db = connect(self.directory)
        try:
            row = db.execute('''SELECT a.* FROM assets a WHERE a.id=? AND a.state='present' AND EXISTS (
                SELECT 1 FROM record_media m JOIN records r ON r.current_revision=m.revision_id
                JOIN record_revisions v ON v.id=r.current_revision
                WHERE m.asset_id=a.id AND r.active=1
                AND COALESCE(json_extract(v.normalized_json,'$.selection.visibility'),'visible')!='hidden'
                AND json_extract(m.usage_json,'$.role') IN ('illustration','evidence_frame'))''', (asset_id,)).fetchone()
        finally:
            db.close()
        if not row:
            raise FileNotFoundError('Unknown image')
        path = (self.directory / row['path']).resolve()
        if not path.is_relative_to(self.directory) or not path.is_file():
            raise FileNotFoundError('Missing image')
        data = path.read_bytes()
        if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256'] or not image_type(data):
            raise FileNotFoundError('Unavailable image')
        return data, image_type(data)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, repository, static=None, **kwargs):
        self.repository = repository
        self.static = Path(static).resolve() if static else None
        super().__init__(*args, directory=str(self.static) if self.static else None, **kwargs)

    def send_payload(self, body, status=200, kind='application/json; charset=utf-8', etag=None):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if etag:
            self.send_header('ETag', etag)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD' and status != 304:
            self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path == '/api/history':
                body, etag = self.repository.snapshot()
                self.send_payload(b'' if self.headers.get('If-None-Match') == etag else body,
                                  304 if self.headers.get('If-None-Match') == etag else 200, etag=etag)
            elif path.startswith('/api/history/assets/'):
                body, kind = self.repository.image(path.removeprefix('/api/history/assets/'))
                self.send_payload(body, kind=kind)
            elif path.startswith('/api/') or not self.static:
                self.send_payload(b'{"error":"not_found"}', 404)
            else:
                target = (self.static / unquote(path).lstrip('/')).resolve()
                if not target.is_relative_to(self.static) or not target.is_file() and not target.is_dir():
                    self.send_payload(b'{"error":"not_found"}', 404)
                elif target.is_dir() and not (target / 'index.html').is_file():
                    self.send_payload(b'{"error":"not_found"}', 404)
                else:
                    super().do_GET() if self.command != 'HEAD' else super().do_HEAD()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (FileNotFoundError, NotADirectoryError):
            self.send_payload(b'{"error":"not_found"}', 404)
        except (sqlite3.Error, ValueError, KeyError, OSError):
            self.send_payload(b'{"error":"history_unavailable"}', 503)

    do_HEAD = do_GET

    def do_POST(self):
        self.send_payload(b'{"error":"read_only"}', 405)

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST


def server(directory, port=0, static=None, host='127.0.0.1'):
    return ThreadingHTTPServer((host, port), partial(Handler, repository=Repository(directory), static=static))


def default_store():
    if (ROOT / 'content/store/history.sqlite3').is_file():
        return ROOT / 'content/store'
    try:
        common = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', '--path-format=absolute', '--git-common-dir'], text=True).strip()
        return Path(common).parent / 'content/store'
    except (OSError, subprocess.CalledProcessError):
        return ROOT / 'content/store'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, default=default_store())
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--static', type=Path)
    args = parser.parse_args()
    # Fail before announcing readiness if the intended database is unavailable.
    db = connect(args.store)
    db.close()
    httpd = server(args.store, args.port, args.static, args.host)
    print(json.dumps({'ready': True, 'port': httpd.server_port}), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == '__main__':
    main()
