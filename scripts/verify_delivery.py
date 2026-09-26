#!/usr/bin/env python3
"""Verify a delivery ZIP without extracting files. Does not approve or publish assets."""
from pathlib import Path
import argparse,hashlib,json,sys,zipfile

def verify(path:Path)->dict:
    with zipfile.ZipFile(path) as archive:
        info=archive.infolist();names=[i.filename for i in info]
        if len(names)!=len(set(names)) or len(info)>1000 or sum(i.file_size for i in info)>512*1024*1024:
            raise ValueError('Unsafe archive sizes or duplicate names')
        for item in info:
            p=Path(item.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in item.filename or ((item.external_attr>>16)&0o170000)==0o120000:
                raise ValueError('Unsafe archive path')
        manifest=json.loads(archive.read('manifest.json'))
        images=manifest['images']
        if manifest['delivered_count']!=len(images):raise ValueError('Image count mismatch')
        expected={x['filename'] for x in images}
        actual={n for n in names if n.startswith('images/')}
        if expected!=actual or len(expected)!=len(images):raise ValueError('Image manifest mismatch')
        for item in images:
            if hashlib.sha256(archive.read(item['filename'])).hexdigest()!=item['sha256']:
                raise ValueError('Image hash mismatch')
        return {'ok':True,'is_demo':manifest['is_demo'],'delivered_count':len(images),
                'warning':'Integrity only; verify current approvals and channel requirements before publishing.'}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('zip');args=parser.parse_args()
    try:print(json.dumps(verify(Path(args.zip)),ensure_ascii=False,indent=2));return 0
    except (OSError,ValueError,KeyError,zipfile.BadZipFile) as exc:
        print(json.dumps({'ok':False,'error':str(exc)}),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
