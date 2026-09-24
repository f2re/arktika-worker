"""Проверка публикации в ЛОКАЛЬНЫЙ bare Git, без обращения к GitHub."""
import hashlib
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[1]/'scripts'/'publish.py'

def load():
 spec=importlib.util.spec_from_file_location('publish_under_test',SCRIPT)
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 return module

@unittest.skipUnless(shutil.which('git'),'Git не установлен; проверка издателя пропущена')
class PublishChecks(unittest.TestCase):
 def test_branch_push_to_local_repository_preserves_main(self):
  with tempfile.TemporaryDirectory() as tmp:
   base=Path(tmp);remote=base/'test-remote.git';seed=base/'seed';source=base/'source';checkout=base/'checkout'
   # Test identity is used only in this isolated local fixture; never in the user's repository.
   env=dict(os.environ,GIT_CONFIG_COUNT='2',GIT_CONFIG_KEY_0='user.name',GIT_CONFIG_VALUE_0='Local test fixture',GIT_CONFIG_KEY_1='user.email',GIT_CONFIG_VALUE_1='fixture@example.invalid')
   def git(*args,cwd=None):return subprocess.run(['git',*args],cwd=cwd,env=env,check=True,capture_output=True,text=True).stdout.strip()
   git('init','--bare',str(remote));git('init','-b','main',str(seed))
   (seed/'README.md').write_text('Base fixture\n');git('add','README.md',cwd=seed);git('commit','-m','Fixture baseline',cwd=seed);git('remote','add','origin',str(remote),cwd=seed);git('push','-u','origin','main',cwd=seed);git('--git-dir',str(remote),'symbolic-ref','HEAD','refs/heads/main')
   old_main=git('--git-dir',str(remote),'rev-parse','main')
   source.mkdir();(source/'README.md').write_text('MVP fixture\n');(source/'program.py').write_text('print("fixture")\n')
   manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
   (source/'MANIFEST.json').write_text(json.dumps({'files':manifest}))
   module=load();module.ROOT=source;module.REPO=str(remote)
   original_git=module.git
   def quiet_git(*args,**kwargs):
    kwargs['capture']=True;return original_git(*args,**kwargs)
   with patch.dict(os.environ,env,clear=True),patch.object(sys,'argv',['publish.py','--checkout',str(checkout),'--push']),patch.object(module,'git',side_effect=quiet_git),contextlib.redirect_stdout(io.StringIO()):module.main()
   self.assertEqual(old_main,git('--git-dir',str(remote),'rev-parse','main'))
   branch='feature/meteo-workstation-v0.2.2'
   self.assertIn('MVP fixture',git('--git-dir',str(remote),'show',branch+':README.md'))
   self.assertIn('program.py',git('--git-dir',str(remote),'ls-tree','--name-only',branch))
 def test_tampered_supply_rejected_before_git(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=Path(tmp)/'source';source.mkdir();(source/'README.md').write_text('changed')
   (source/'MANIFEST.json').write_text(json.dumps({'files':{'README.md':'0'*64}}));module=load();module.ROOT=source
   with patch.object(sys,'argv',['publish.py','--checkout',str(Path(tmp)/'target')]),patch.object(module,'git') as git:
    with self.assertRaises(SystemExit):module.main()
    git.assert_not_called()
