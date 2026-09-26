#!/usr/bin/env python3
"""Пересобрать автономное руководство: python -m pip install 'mistune>=3,<4'.

Для запуска самого приложения mistune не требуется: docs/index.html включён в поставку.
"""
from html import escape
from pathlib import Path
import re
import mistune
ROOT = Path(__file__).resolve().parents[1]
PAGES = [
 ('README.md','Обзор'), ('docs/INSTALL.md','Установка'), ('docs/AUTH.md','Авторизация'),
 ('docs/USER_GUIDE.md','Работа с приложением'), ('docs/ANALYSIS.md','Контекстный анализ'), ('docs/DESIGN.md','Дизайн'), ('docs/SCIENCE.md','Методики'),
 ('docs/API.md','API'), ('docs/DATA_MODEL.md','Модель данных'),
 ('docs/ARCHITECTURE.md','Архитектура'), ('docs/ROADMAP.md','Развитие ГИС'),
 ('docs/KNOWN_LIMITS.md','Ограничения'), ('docs/VERIFICATION.md','Проверка'), ('docs/AUDIT.md','Аудит'),
 ('docs/PUBLISH.md','Публикация'), ('docs/SOURCES.md','Источники')]
CSS='''
:root{color-scheme:light}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f4f7fa;color:#183246;font:16px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}a{color:#047c87}nav{position:fixed;inset:0 auto 0 0;width:250px;padding:28px 22px;background:#163647;color:#fff;overflow:auto}nav strong{display:block;font-size:22px;line-height:1.3;margin-bottom:24px}nav a{display:block;color:#d8e9ef;text-decoration:none;padding:7px 0}nav a:hover{color:#fff;text-decoration:underline}nav small{display:block;color:#9bbdc7;margin-top:25px}main{margin-left:250px;padding:36px;max-width:1450px}section{scroll-margin-top:24px;background:white;padding:32px 40px;margin:0 0 28px;border:1px solid #dbe4eb;border-radius:12px}h1,h2,h3{line-height:1.3;overflow-wrap:anywhere}h1{font-size:29px;margin-top:0}h2{margin-top:30px;font-size:22px}h3{font-size:18px}table{border-collapse:collapse;display:block;overflow:auto;width:100%;font-size:14px}td,th{padding:10px 12px;border:1px solid #dce5eb;text-align:left;vertical-align:top}th{background:#ecf5f6}pre{background:#eef3f7;padding:18px;overflow:auto;border-radius:6px;line-height:1.5;font-size:13px}code{font-family:ui-monospace,monospace;font-size:.91em}p code,li code{background:#edf2f6;border-radius:3px;padding:2px 4px}img{max-width:100%;height:auto;border:1px solid #dbe4eb}blockquote{margin-left:0;border-left:4px solid #06919b;padding:8px 18px;background:#f0f8f8}ul,ol{padding-left:26px}li{margin:5px 0}.source{color:#6c8495;font-size:13px}a{overflow-wrap:anywhere}@media(max-width:850px){nav{position:relative;width:100%}nav a{display:inline-block;margin-right:15px}main{margin:0;padding:16px}section{padding:22px}}@media print{nav{display:none}main{margin:0;padding:0}section{border:0;break-before:page;padding:0}section:first-child{break-before:auto}pre{white-space:pre-wrap}a{color:inherit}}
'''

def main():
 md=mistune.create_markdown(escape=True,plugins=['table','strikethrough'])
 links='';sections=[]
 mapping={Path(path).name.replace('.md',''):Path(path).stem.lower() for path,_ in PAGES}
 for path,title in PAGES:
  slug=Path(path).stem.lower();links+='<a href="#'+slug+'">'+escape(title)+'</a>'
  body=md((ROOT/path).read_text(encoding='utf-8'))
  body=body.replace('src="docs/interface.png"','src="interface.png"')
  def local_link(m):
   href=m.group(1)
   if href.startswith(('https:','http:','#','mailto:')):return m.group(0)
   stem=Path(href.split('#')[0]).stem
   if stem in mapping:return 'href="#'+mapping[stem]+'"'
   if href.startswith('docs/') and href.endswith('.html'):return 'href="'+href[5:]+'"'
   return m.group(0)
  body=re.sub(r'href="([^"]+)"',local_link,body)
  sections.append('<section id="'+slug+'">'+body+'</section>')
 links+='<a href="ERA5.html">ERA5 и опорная шкала</a>'
 doc='<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Арктика-М — руководство и план ГИС</title><style>'+CSS+'</style></head><body><nav><strong>АРКТИКА-М<br>Рабочее место метеоролога</strong>'+links+'<small>Версия 0.2.2<br>Автономное руководство<br>Наблюдения ≠ прогноз</small></nav><main>'+''.join(sections)+'</main></body></html>'
 (ROOT/'docs/index.html').write_text(doc,encoding='utf-8')
 print('docs/index.html создан: '+str(len(doc.encode('utf-8')))+' байт')
if __name__=='__main__':main()
