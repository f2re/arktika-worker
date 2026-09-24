#!/usr/bin/env python3
"""Контрольные суммы исходной поставки; runtime и пользовательские данные исключены."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git','.venv','__pycache__','node_modules','.pytest_cache','browser-results','.delivery'}
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    if args.check:
        expected=json.loads((ROOT/'MANIFEST.json').read_text())['files']
        bad=[n for n,h in expected.items() if not (ROOT/n).is_file() or hashlib.sha256((ROOT/n).read_bytes()).hexdigest()!=h]
        if bad:raise SystemExit('Контрольные суммы не совпали: '+', '.join(bad))
        print('Verified',len(expected),'source files');return
    previous = json.loads((ROOT/'MANIFEST.json').read_text()) if (ROOT/'MANIFEST.json').exists() else {}
    names = set(previous.get('files', {}))
    # Source-only directories; never recursively include the runtime state tree.
    for folder in ('arktika','static','docs','tests','scripts','config','examples','.github'):
        for p in (ROOT/folder).rglob('*'):
            if p.is_file() and not any(part in SKIP for part in p.relative_to(ROOT).parts):
                if p.name.endswith(('.py','.js','.css','.html','.md','.svg','.geojson','.yml','.json','.sh','.cmd','.txt','.example')):
                    if not p.name.endswith(('.local.json','.download.json')) and p.name != 'app.json':
                        names.add(p.relative_to(ROOT).as_posix())
    for p in ROOT.iterdir():
        if p.is_file() and (p.suffix in ('.py','.md','.txt','.cmd','.sh','.command') or p.name in ('LICENSE','.gitignore','.gitattributes')):
            names.add(p.name)
    def eligible(name):
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or any(part in SKIP for part in path.parts):
            return False
        if path.parts[0]=='config' and not name.endswith(('.example.json','.service.example')):
            return False
        if path.parts[0]=='examples' and not (path.name.endswith('_schema.json') or path.name=='README.md'):
            return False
        return name!='MANIFEST.json' and not (ROOT/name).is_symlink() and (ROOT/name).is_file()
    files = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
             for name in sorted(names) if eligible(name)}
    result = {'version':'0.2.2','files':files}
    (ROOT/'MANIFEST.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(len(files),'source files')
if __name__ == '__main__':
    main()
