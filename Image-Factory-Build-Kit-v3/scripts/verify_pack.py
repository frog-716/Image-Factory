"""Verify package manifest and local Markdown links. Does not access user project files."""
import hashlib
import json
import re
from pathlib import Path
root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'PACKAGE-MANIFEST.json').read_text(encoding='utf-8'))
for entry in manifest['files']:
    path=(root/entry['path']).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise SystemExit('Missing/unsafe package member: '+entry['path'])
    if hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:
        raise SystemExit('Package file changed: '+entry['path'])
errors=[]
for path in root.rglob('*.md'):
    for link in re.findall(r'\]\(([^)]+)\)',path.read_text(encoding='utf-8')):
        if '://' in link or link.startswith(('#','mailto:')): continue
        rel=link.split('#')[0]
        if rel and not (path.parent/rel).exists(): errors.append(f'{path.relative_to(root)} -> {rel}')
if errors: raise SystemExit('\n'.join(errors))
print(f'PASS: {len(manifest["files"])} package hashes; local Markdown links valid')
