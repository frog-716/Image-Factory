"""Deterministic delivery of reviewed bytes; audit files are separate from image count."""
import io
import json
import zipfile
from .contracts import Blocked, Conflict, canonical, digest, identifier, sha
from .review import validate_review_snapshot

def build_zip(snapshot: dict, assets: dict[str,dict]) -> tuple[bytes,dict]:
    verdict = validate_review_snapshot(snapshot,snapshot['mode'])
    if not verdict['approved']:
        raise Blocked('No approved images; do not create a fake successful empty delivery')
    entries, images = [], {}
    hashes = set()
    for aid in sorted(verdict['approved']):
        a = assets.get(aid)
        if not a or a.get('mode')!=snapshot['mode'] or a.get('run_id')!=snapshot['run_id']:
            raise Blocked('Asset missing or cross-run/cross-mode')
        expected = next(c['sha256'] for c in snapshot['candidates'] if c['asset_id']==aid)
        content = a.get('content')
        if not isinstance(content,bytes) or sha(content)!=expected:
            raise Conflict('Approved bytes changed')
        if expected in hashes:
            raise Conflict('Same image cannot occupy two candidate slots')
        hashes.add(expected)
        name = identifier(a.get('name'))
        if not name.lower().endswith(('.png','.jpg','.jpeg','.webp')):
            raise Conflict('Not an image filename')
        if name in images:
            raise Conflict('Duplicate delivery filename')
        images[name] = content
        # Allowlist, not whole-record serialization: omit tokens, personal details,
        # temporary attachment URLs and internal prompt traces.
        entries.append({'asset_id':aid,'file':name,'sha256':expected,
                        'review_revision':verdict['latest'][aid]['revision'],
                        'origin':a.get('origin','not_reported')})
    manifest = {'schema_version':1,'run_id':snapshot['run_id'],'mode':snapshot['mode'],
                'review_snapshot_hash':digest(snapshot),'image_count':len(entries),'images':entries}
    label = 'DEMO ONLY. Fictional assets. No real product/brand/channel claims.\n' if snapshot['mode']=='demo' else 'Approved assets. Use only within the reviewed channel scope.\n'
    items = dict(images)
    items['manifest.json'] = canonical(manifest).encode('utf-8')
    items['README.txt'] = label.encode('utf-8')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in sorted(items.items()):
            info = zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info,data)
    result = stream.getvalue()
    verify_zip(result,manifest)
    return result,manifest

def verify_zip(data: bytes, expected_manifest: dict) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        expected = {x['file'] for x in expected_manifest['images']}|{'README.txt','manifest.json'}
        if len(names)!=len(set(names)) or set(names)!=expected:
            raise Conflict('Unexpected/missing/duplicate files in ZIP')
        actual = json.loads(z.read('manifest.json'))
        if actual!=expected_manifest:
            raise Conflict('ZIP manifest mismatch')
        for item in expected_manifest['images']:
            if sha(z.read(item['file']))!=item['sha256']:
                raise Conflict('ZIP image hash mismatch')
