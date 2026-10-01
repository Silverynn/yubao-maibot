"""无pytest依赖，执行交付的原生恢复测试原文断言；需完整MaiBot环境。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import ast
import asyncio
import unittest
import uuid

ROOT=Path(__file__).resolve().parent

def load_assertions(path, case):
 # 仅移除pytest框架导入及装饰器，函数正文/断言原样保留。
 tree=ast.parse(path.read_text(encoding='utf-8-sig'))
 tree.body=[n for n in tree.body if not (isinstance(n,ast.Import) and any(i.name=='pytest' for i in n.names))]
 for node in tree.body:
  if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):node.decorator_list=[]
 scope={'__file__':str(path),'pytest':SimpleNamespace(raises=case.assertRaises)}
 exec(compile(tree,str(path),'exec'),scope)
 return scope

class Recovery(unittest.TestCase):
 def setUp(self):
  self.directory=ROOT/'.runtime/test-data'/('native-'+uuid.uuid4().hex)
  self.directory.mkdir(parents=True)
  self.namespace=load_assertions(ROOT/'pytests/A_memorix_test/test_empty_vector_recovery_regression.py',self)
  self.request=SimpleNamespace(addfinalizer=self.addCleanup)
  def set_attr(obj,name,value):
   ctx=patch.object(obj,name,value);ctx.start();self.addCleanup(ctx.stop)
  self.monkey=SimpleNamespace(setattr=set_attr)
 def test_empty_without_metadata(self):
  self.namespace['test_verified_empty_storage_creates_reloadable_dual_pools'](self.directory,False,self.request)
 def test_empty_old_metadata(self):
  self.namespace['test_verified_empty_storage_creates_reloadable_dual_pools'](self.directory,True,self.request)
 def test_nonempty_legacy(self):self.nonempty('')
 def test_nonempty_paragraph(self):self.nonempty('paragraph')
 def test_nonempty_graph(self):self.nonempty('graph')
 def nonempty(self,pool):self.namespace['test_nonempty_storage_is_never_overwritten_by_empty_initialization'](self.directory,pool,self.request)
 def test_corrupt_not_replaced(self):self.namespace['test_corrupt_empty_metadata_is_not_replaced'](self.directory,self.request)
 def test_persist_runtime(self):self.namespace['test_summary_persists_current_runtime_even_with_stale_none_reference']()
 def test_persist_graph(self):self.namespace['test_standalone_summary_persists_graph_when_vectors_are_degraded']()
 def test_persist_dual(self):self.namespace['test_standalone_summary_saves_both_active_vector_pools']()
 def test_recovery_vectors(self):asyncio.run(self.namespace['test_embedding_recovery_reenables_empty_storage_and_persists_new_vectors'](self.directory,self.monkey,self.request))

class Cleanup(unittest.TestCase):
 def setUp(self):self.namespace=load_assertions(ROOT/'pytests/A_memorix_test/test_storage_cleanup_startup_wait.py',self)
 def test_verified_resume(self):asyncio.run(self.namespace['test_cleanup_waits_without_claiming_and_resumes_after_verification'](''))
 def test_other_error_resume(self):asyncio.run(self.namespace['test_cleanup_waits_without_claiming_and_resumes_after_verification']('vector_unclassified_error'))
 def test_mismatch_resume(self):asyncio.run(self.namespace['test_cleanup_waits_without_claiming_and_resumes_after_verification']('v2_fingerprint_mismatch'))
 def test_stop_while_waiting(self):asyncio.run(self.namespace['test_cleanup_can_stop_while_waiting_for_verification']())

if __name__=='__main__':unittest.main()
