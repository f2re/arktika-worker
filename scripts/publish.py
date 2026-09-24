#!/usr/bin/env python3
"""Внести поставку в отдельную Git-ветку. Не хранит и не запрашивает токен GitHub.

Использует обычную настроенную аутентификацию git на машине владельца.
Никогда не делает force push и не меняет удалённую main напрямую.
"""
import argparse,hashlib,json,shutil,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REPO='https://github.com/f2re/arktika-worker.git'
def git(*args,cwd=None,capture=False):
 return subprocess.run(['git',*args],cwd=cwd,check=True,text=True,capture_output=capture)
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--checkout',required=True,help='Отдельная папка клона, не папка распакованной поставки');p.add_argument('--branch',default='feature/meteo-workstation-v0.2.2');p.add_argument('--push',action='store_true');args=p.parse_args()
 manifest=json.loads((ROOT/'MANIFEST.json').read_text())['files']
 for name,expected in manifest.items():
  path=ROOT/name
  if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise SystemExit('Поставка изменена или повреждена: '+name)
 target=Path(args.checkout).expanduser().resolve()
 if target==ROOT or ROOT in target.parents or target in ROOT.parents:raise SystemExit('Нужна отдельная папка клона, вне дерева поставки.')
 if not target.exists():git('clone',REPO,str(target))
 if not (target/'.git').is_dir():raise SystemExit('В указанной папке нет обычного Git-клона.')
 remote=git('remote','get-url','origin',cwd=target,capture=True).stdout.strip()
 if remote.rstrip('/') not in (REPO,REPO[:-4],'git@github.com:f2re/arktika-worker.git'):raise SystemExit('origin не соответствует f2re/arktika-worker. Изменения не внесены.')
 if git('status','--porcelain',cwd=target,capture=True).stdout.strip():raise SystemExit('В клоне есть несохранённые изменения. Сначала сохраните их.')
 git('fetch','origin',cwd=target)
 if not git('config','user.name',cwd=target,capture=True).stdout.strip():raise SystemExit('Настройте git user.name.')
 if not git('config','user.email',cwd=target,capture=True).stdout.strip():raise SystemExit('Настройте git user.email.')
 # Refuse unexpected code collisions. The README is the intended existing entry point.
 tracked=set(git('ls-tree','-r','--name-only','origin/main',cwd=target,capture=True).stdout.splitlines())
 conflicts=(tracked&set(manifest))-{'README.md'}
 if conflicts:raise SystemExit('В main уже появились файлы реализации. Автоматическое наложение отменено: '+', '.join(sorted(conflicts)[:20]))
 git('switch','-c',args.branch,'origin/main',cwd=target)
 for name in manifest:
  dst=target/name;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,dst)
 # This manifest is verified above but intentionally not self-hashed.
 shutil.copy2(ROOT/'MANIFEST.json',target/'MANIFEST.json')
 git('add','--all',cwd=target)
 git('commit','-m','Add Arktika-M local meteorologist workstation MVP and documentation',cwd=target)
 if args.push:
  git('push','--set-upstream','origin',args.branch,cwd=target)
  print('Ветка опубликована. Создайте pull request:')
  print('https://github.com/f2re/arktika-worker/compare/main...'+args.branch)
 else:print('Локальный коммит готов. Для публикации: git -C "'+str(target)+'" push -u origin '+args.branch)
if __name__=='__main__':
 try:main()
 except subprocess.CalledProcessError as e:raise SystemExit('Команда git завершилась ошибкой. Проверьте доступ GitHub и настройки git; токены в чат не передавайте. Код: '+str(e.returncode))
