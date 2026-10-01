"""交付安装安全测试：在虚构目标中运行，不修改真实机器人。"""
from pathlib import Path
from unittest.mock import patch
import hashlib
import importlib.util
import json
import unittest
import uuid

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('installer',ROOT/'install_delivery.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)

class SafetyTests(unittest.TestCase):
 def setUp(self):
  self.target=ROOT/'.runtime/test-data'/uuid.uuid4().hex
  (self.target/'src').mkdir(parents=True)
 def test_default_excludes_mood_native_frontend(self):
  rows=installer.plan(self.target)
  self.assertFalse(any(r['component'] in {'mood','native','dashboard'} for r in rows))
  self.assertFalse(any(r['target'].startswith('vtuber/') or r['target'].endswith('config.toml') for r in rows))
 def test_unknown_changes_block_entire_apply(self):
  file=self.target/'plugins/heart_memory_audit/plugin.py';file.parent.mkdir(parents=True);file.write_text('# local edit',encoding='utf-8')
  rows=installer.plan(self.target)
  with self.assertRaisesRegex(ValueError,'未知'):installer.apply(self.target,rows,True,True)
  self.assertEqual(file.read_text(),'# local edit')
  self.assertFalse((self.target/'heart_shared').exists())
 def test_custom_mood_preserved_and_explicit_selection_blocked(self):
  file=self.target/'plugins/heart_mood/plugin.py';file.parent.mkdir(parents=True);file.write_text('# partner mood',encoding='utf-8')
  self.assertFalse(any(r['target']==str(file.relative_to(self.target)).replace('\\','/') for r in installer.plan(self.target)))
  rows=installer.plan(self.target,include_mood=True)
  with self.assertRaises(ValueError):installer.apply(self.target,rows,True,True)
  self.assertEqual(file.read_text(),'# partner mood')
 def test_requires_stopped_and_shared_review(self):
  rows=installer.plan(self.target)
  with self.assertRaisesRegex(ValueError,'关闭'):installer.apply(self.target,rows,False,True)
  with self.assertRaisesRegex(ValueError,'共享库'):installer.apply(self.target,rows,True,False)
 def test_path_escape_rejected(self):
  with self.assertRaises(ValueError):installer.safe_target(self.target,'../escape')
 def test_backups_preserves_config_and_no_database(self):
  config=self.target/'plugins/heart_memory_audit/config.toml';config.parent.mkdir(parents=True);config.write_text('local_config=true',encoding='utf-8')
  rows=installer.plan(self.target)
  installer.apply(self.target,rows,True,True)
  self.assertEqual(config.read_text(),'local_config=true')
  self.assertFalse((self.target/'data').exists())
  self.assertEqual(hashlib.sha256((self.target/'plugins/heart_memory_audit/plugin.py').read_bytes()).hexdigest(),next(r['sha256'] for r in rows if r['target']=='plugins/heart_memory_audit/plugin.py'))

if __name__=='__main__':unittest.main()
