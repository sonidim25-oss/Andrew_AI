#!/usr/bin/env python3
"""Bind a normalized imported manifest to the release's content-addressed config.
OCI loading can change compression/media types and therefore the manifest digest.
The config digest pins image settings and uncompressed rootfs layer content.
"""
import json
from pathlib import Path
import re
import sys

def imported_reference(release, inspection):
    source=release.get('image','')
    config=release.get('image_config_id','')
    if not re.fullmatch(r'localhost/andrew-browser@sha256:[0-9a-f]{64}',source):
        raise ValueError('invalid release image reference')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',config):
        raise ValueError('release content identity missing')
    if not isinstance(inspection,list) or len(inspection)!=1 or not isinstance(inspection[0],dict):
        raise ValueError('one imported image required')
    image=inspection[0]
    identifier=image.get('Id','')
    if not isinstance(identifier,str) or identifier.removeprefix('sha256:')!=config.removeprefix('sha256:'):
        raise ValueError('imported image content does not match release')
    digest=image.get('Digest','')
    if not isinstance(digest,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',digest):
        raise ValueError('imported immutable digest unavailable')
    return source.split('@',1)[0]+'@'+digest

if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(256*1024+1)
        if len(raw)>256*1024: raise ValueError('image inspection too large')
        print(imported_reference(json.loads(Path(sys.argv[1]).read_text()),json.loads(raw)))
    except (OSError,ValueError,TypeError):
        print('Imported image identity validation failed; no metadata or credentials printed.',file=sys.stderr)
        raise SystemExit(1)
