"""Deterministic public read model; the SQLite and research blobs stay private."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from history_api import connect, default_store, image_type, project
from history_store import ROOT, dump, validate_contract

PRIVATE = re.compile(r'/Users/|/home/|/workspace/|BEGIN (?:RSA |EC )?PRIVATE KEY|(?:access_token|refresh_token|appSecret)"\s*:\s*"[^"\s]+', re.I)
PUBLIC_URL = re.compile(r'https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+')
EXTENSIONS = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}
VERSION_PATH = 'api/history/version.json'


def open_license(value):
    text = str(value or '').lower().replace('-', ' ')
    return bool(re.search(r'\bcc\s*(?:by(?:\s*sa)?|0)\b', text) and not re.search(r'\b(?:nc|nd)\b', text) or text.startswith('public domain') or text.startswith('pd '))


def body_permitted(record, metadata):
    if record['selection'].get('public_body') is False:
        return False
    return (record['selection'].get('public_body') is True or
            open_license(record['selection'].get('text_license') or metadata.get('license')) or
            metadata.get('license') in (
                'NIST非SRD作品：美国政府作品及全球免版税衍生/再发布许可；非CC0',
                'NASA原创政府文字的事实性教育/信息使用许可；非CC0',
                'Public Data License 1.0; exhibition text and images explicitly permit commercial reuse with attribution'))


def media_permitted(media):
    rights = media['rights']
    usage = rights.get('usage_basis') or {}
    nasa_information = (usage.get('kind') == 'nasa_educational_information' and
        usage.get('guidelines_url') == 'https://www.nasa.gov/nasa-brand-center/images-and-media/' and
        str(usage.get('source_page', '')).startswith('https://www.nasa.gov/image-article/') and
        usage.get('image_credit') == 'NASA' and usage.get('third_party_credit') is False and
        usage.get('reviewed_on') and rights.get('public_use') is True)
    return rights.get('public_use') is not False and rights.get('status') not in ('local_research_only', 'reuse_permission_unverified') and (open_license(rights.get('license')) or bool(nasa_information))


def private_material(value):
    # A public HTTPS route such as /home/journal is not a local home folder.
    # Check every remaining character, including credentials and local paths in
    # URL query parameters, rather than weakening the publication gate.
    def redact_url(match):
        from urllib.parse import urlsplit
        url = match.group()
        parts = urlsplit(url)
        if parts.username or parts.password or PRIVATE.search(parts.query):
            return url
        return '[public-url]'
    return PRIVATE.search(PUBLIC_URL.sub(redact_url, value)) or re.search(
        r'BEGIN (?:RSA |EC )?PRIVATE KEY|(?:access_token|refresh_token|appSecret)"\s*:\s*"[^"\s]+', value.replace('\\"', '"'), re.I)


def public_locator(value):
    try:
        locator = json.loads(value)
    except (ValueError, TypeError):
        return value if not PRIVATE.search(value) else '参见来源原文'
    if not isinstance(locator, dict):
        return '参见来源原文'
    # Preserve useful page/time references; frame paths and internal hashes stay private.
    visible = {k: v for k, v in locator.items() if k in ('start_seconds', 'end_seconds', 'time', 'page', 'paragraph', 'locator') and isinstance(v, (str, int, float))}
    return dump(visible) if visible else '参见来源原文'


@dataclass
class PublicRelease:
    data: dict
    files: dict[str, bytes]
    manifest: dict
    report: dict


def build_public(directory, retain_images=True):
    directory = Path(directory).resolve()
    db = connect(directory)
    try:
        db.execute('BEGIN')
        data = project(db)
        rows = {r['id']: r for r in db.execute('''SELECT v.id,v.normalized_json,sv.metadata_json FROM records r
            JOIN record_revisions v ON v.id=r.current_revision JOIN source_versions sv ON sv.id=v.source_version_id WHERE r.active=1''')}
        assets = {r['id']: r for r in db.execute('SELECT * FROM assets')}
    finally:
        db.close()
    files = {}
    report = {'withheld_media': 0, 'missing_media': 0, 'withheld_bodies': 0}
    public_sources, source_ids = {}, {}
    for source in data['sources']:
        descriptor = {k: v for k, v in source.items() if k != 'id'}
        source_id = 'PSRC-' + hashlib.sha256(dump(descriptor).encode()).hexdigest()[:24]
        public_sources[source_id] = {'id': source_id, **descriptor}
        source_ids[source['id']] = source_id
    data['sources'] = sorted(public_sources.values(), key=lambda source: source['id'])
    for entry in data['events']:
        row = rows[entry['revision_id']]
        record, metadata = json.loads(row['normalized_json']), json.loads(row['metadata_json'])
        # Research/audit notes can contain local paths. Public copy uses fixed notices.
        entry['notes'] = []
        if entry['body'] and not body_permitted(record, metadata):
            entry['body'] = []
            entry['content_notice'] = '此来源全文暂未公开，可通过来源链接继续阅读。'
            report['withheld_bodies'] += 1
        approved = {m['asset_id'] for m in record['media'] if media_permitted(m)}
        public_images = []
        for image in entry['images']:
            asset_id = 'AS-' + image['sha256']
            if asset_id not in approved:
                report['withheld_media'] += 1
                continue
            asset = assets[asset_id]
            path = (directory / asset['path']).resolve()
            if not path.is_relative_to(directory) or not path.is_file():
                report['missing_media'] += 1
                continue
            raw = path.read_bytes()
            kind = image_type(raw)
            if kind not in EXTENSIONS or len(raw) != asset['bytes'] or hashlib.sha256(raw).hexdigest() != asset['sha256']:
                raise ValueError('Public media integrity mismatch: ' + asset_id)
            name = 'api/history/assets/' + asset_id + '.' + EXTENSIONS[kind]
            if retain_images:
                files[name] = raw
            image['src'] = '/' + name
            public_images.append(image)
        if len(public_images) != len(entry['images']):
            entry['notes'].append('部分配图尚未确认公开使用许可或本地文件暂不可用，当前未公开展示。')
        entry['images'] = public_images
        citations = {}
        for citation in entry['citations']:
            citation.update(source_id=source_ids[citation['source_id']], locator=public_locator(citation['locator']), block_sha256=None)
            citations[dump(citation)] = citation
        entry['citations'] = list(citations.values())
    data['scope'] = 'public_database'
    data.pop('revision')
    data['revision'] = hashlib.sha256(dump(data).encode()).hexdigest()
    raw = dump(data).encode()
    if private_material(raw.decode()):
        raise ValueError('Private path or credential material in public response')
    validate_contract(data, json.loads((ROOT / 'content/store/frontend.schema.json').read_text()))
    snapshot = 'api/history/snapshots/' + data['revision'] + '.json'
    files[snapshot] = raw
    manifest = {'schema_version': '1.0.0', 'revision': data['revision'], 'url': '/' + snapshot,
                'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    files[VERSION_PATH] = dump(manifest).encode()
    report.update(counts=data['counts'], public_images=len({i['src'] for e in data['events'] for i in e['images']}),
                  snapshot_bytes=len(raw), revision=data['revision'])
    return PublicRelease(data, files, manifest, report)


def write_release(release, directory):
    directory = Path(directory).resolve()
    for name, raw in release.files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_bytes(raw)
        temporary.replace(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, default=default_store())
    parser.add_argument('--output', type=Path, default=ROOT / 'prototype/dist/live-data')
    args = parser.parse_args()
    release = build_public(args.store)
    write_release(release, args.output)
    print(json.dumps(release.report, ensure_ascii=False))
