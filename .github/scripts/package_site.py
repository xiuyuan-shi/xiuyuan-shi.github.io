"""Package only approved static site files and the public data read model."""
import hashlib
import json
from pathlib import Path
import re
import shutil

API_FILE = re.compile(r'api/history/(?:version\.json|snapshots/[a-f0-9]{64}\.json|assets/AS-[a-f0-9]{64}\.(?:jpg|png|webp))$')
PRIVATE = re.compile(r'/Users/|/home/|/workspace/|BEGIN (?:RSA |EC )?PRIVATE KEY|(?:access_token|refresh_token|appSecret)"\s*:\s*"[^"\s]+', re.I)


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
    release = json.loads((source / 'public-release.json').read_text())
    for name, digest in release['files'].items():
        if not eligible(name) or hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError('Application file hash mismatch')
    chosen = []
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('Symlinks cannot enter a Pages artifact')
        if not path.is_file() or output in path.parents:
            continue
        name = path.relative_to(source).as_posix()
        if not eligible(name):
            continue
        if path.suffix in ('.json', '.html', '.js', '.css', '.svg', '.txt') and PRIVATE.search(path.read_text()):
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
