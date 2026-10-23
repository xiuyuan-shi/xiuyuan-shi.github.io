"""Package only approved static site files and the public data read model."""
import hashlib
import json
from pathlib import Path
import re
import shutil

API_FILE = re.compile(r'api/history/(?:version\.json|snapshots/[a-f0-9]{64}\.json|assets/AS-[a-f0-9]{64}\.(?:jpg|png|webp)|reader/[a-f0-9]{64}/(?:manifest\.json|catalog\.json|windows/-?\d+\.json|details/[a-f0-9]{2}\.json))$')
PRIVATE = re.compile(r'/Users/|/home/|/workspace/|BEGIN (?:RSA |EC )?PRIVATE KEY|(?:access_token|refresh_token|appSecret)"\s*:\s*"[^"\s]+', re.I)
PUBLIC_URL = re.compile(r'https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+')


def private_material(value):
    # Public web routes are not local folders; credential text and local paths
    # outside a URL (or inside its query) still prevent publication.
    def redact_url(match):
        from urllib.parse import urlsplit
        url = match.group()
        parts = urlsplit(url)
        if parts.username or parts.password or PRIVATE.search(parts.query):
            return url
        return '[public-url]'
    return PRIVATE.search(PUBLIC_URL.sub(redact_url, value)) or re.search(
        r'BEGIN (?:RSA |EC )?PRIVATE KEY|(?:access_token|refresh_token|appSecret)"\s*:\s*"[^"\s]+', value.replace('\\"', '"'), re.I)


def eligible(name):
    if name.startswith('.') or '\\' in name or '..' in name.split('/'):
        return name == '.nojekyll'
    if name.startswith('api/'):
        return bool(API_FILE.fullmatch(name))
    if name.startswith('assets/'):
        return not any(p.startswith('.') for p in name.split('/')) and not name.endswith(('.provenance.json', '.txt', '.map', '.key', '.pem', '.sqlite3', '.db'))
    if name.startswith('audio/'):
        return name == 'audio/dream-of-hexi-corridor-96k.mp3'
    if name.startswith('data/'):
        return name.split('/', 1)[1] in ('history-trial.json', 'history-map.json', 'content-receipt.json', 'map-receipt.json', 'music-source.json')
    return name in ('index.html', 'explore.html', 'prototype.html', 'favicon.svg', 'public-release.json', 'github-pages-release.json',
                    'explore/index.html', 'prototype/index.html', 'licenses/Inter.txt', 'licenses/Roboto.txt', 'licenses/Three.txt')


def package(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output == source or source.is_relative_to(output):
        raise ValueError('Unsafe package destination')
    version = json.loads((source / 'api/history/version.json').read_text())
    expected = '/api/history/snapshots/' + version['revision'] + '.json'
    if version['url'] != expected or not re.fullmatch('[a-f0-9]{64}', version['revision']):
        raise ValueError('Invalid snapshot reference')
    raw = (source / version['url'].lstrip('/')).read_bytes()
    if len(raw) != version['bytes'] or hashlib.sha256(raw).hexdigest() != version['sha256']:
        raise ValueError('Public snapshot hash mismatch')
    data = json.loads(raw)
    if data['scope'] != 'public_database' or data['revision'] != version['revision']:
        raise ValueError('Snapshot scope or revision mismatch')
    for event in data['events']:
        for image in event['images']:
            name = image['src'].lstrip('/')
            if not API_FILE.fullmatch(name) or hashlib.sha256((source / name).read_bytes()).hexdigest() != image['sha256']:
                raise ValueError('Public image hash mismatch')
    if version.get('reader'):
        prefix = '/api/history/reader/' + version['revision'] + '/'
        def checked_ref(ref, expected):
            if ref.get('url') != prefix + expected or not API_FILE.fullmatch(ref['url'].lstrip('/')):
                raise ValueError('Invalid reader resource path')
            raw = (source / ref['url'].lstrip('/')).read_bytes()
            if len(raw) != ref['bytes'] or hashlib.sha256(raw).hexdigest() != ref['sha256']:
                raise ValueError('Reader resource hash mismatch')
            return json.loads(raw)
        reader = checked_ref(version['reader'], 'manifest.json')
        if reader['schema_version'] != '1.0.0' or reader['revision'] != version['revision'] or reader['window_years'] != 50:
            raise ValueError('Invalid reader manifest')
        catalog = checked_ref(reader['catalog'], 'catalog.json')
        if catalog['counts']['total'] != len([e for e in data['events'] if e['content_status'] != 'candidate']):
            raise ValueError('Reader catalogue count mismatch')
        for group in ('windows', 'details'):
            for key, ref in reader[group].items():
                value = checked_ref(ref, group + '/' + key + '.json')
                if value['revision'] != version['revision'] or any(e['content_status'] == 'candidate' for e in value['events']):
                    raise ValueError('Invalid reader content scope')
    release = json.loads((source / 'public-release.json').read_text())
    for name, digest in release['files'].items():
        if not eligible(name) or hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError('Application file hash mismatch')
    pages = json.loads((source / 'github-pages-release.json').read_text())
    aliases = pages.get('aliases', {})
    for name, digest in aliases.items():
        if not eligible(name) or hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError('Application alias hash mismatch')
    approved_app = set(release['files']) | set(aliases) | {'public-release.json', 'github-pages-release.json', '.nojekyll'}
    chosen = []
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('Symlinks cannot enter a Pages artifact')
        if not path.is_file() or output in path.parents:
            continue
        name = path.relative_to(source).as_posix()
        if not eligible(name) or (not API_FILE.fullmatch(name) and name not in approved_app):
            continue
        if path.suffix in ('.json', '.html', '.js', '.css', '.svg', '.txt') and private_material(path.read_text()):
            raise ValueError('Private material in site artifact')
        chosen.append((path, name))
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for path, name in chosen:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return {'files': len(chosen), 'revision': version['revision'], 'counts': data['counts']}


if __name__ == '__main__':
    import sys
    print(json.dumps(package(Path.cwd(), Path(sys.argv[1] if len(sys.argv) > 1 else '_site'))))
