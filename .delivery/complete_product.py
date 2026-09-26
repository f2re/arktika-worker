"""Apply the reviewed UTF-8 edits only when every before/after digest matches.

One-time delivery helper. Removed from the branch before normal CI and merge.
"""
import hashlib
import json
from pathlib import Path
import subprocess

root=Path.cwd().resolve()
changes=json.loads(Path('.delivery/product-edits.json').read_text(encoding='utf-8'))
outputs={}
for name,change in changes.items():
    path=(root/name).resolve()
    if not path.is_relative_to(root) or path.is_symlink():
        raise SystemExit('Invalid source path')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=change['before']:
        raise SystemExit('Source changed: '+name)
    lines=raw.decode('utf-8').splitlines(keepends=True)
    for edit in reversed(change['edits']):
        if not 0<=edit['start']<=edit['stop']<=len(lines):
            raise SystemExit('Invalid edit: '+name)
        lines[edit['start']:edit['stop']]=[edit['text']]
    result=''.join(lines).encode('utf-8')
    if hashlib.sha256(result).hexdigest()!=change['after']:
        raise SystemExit('Result mismatch: '+name)
    outputs[path]=result
for path,data in outputs.items():
    path.write_bytes(data)
for name in ('.delivery/product-edits.json','.delivery/complete_product.py','.github/workflows/complete-product-flow.yml'):
    (root/name).unlink()
subprocess.run(['python','scripts/manifest.py'],check=True)
print('Applied and verified',len(outputs),'source files')
