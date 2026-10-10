"""仅运行模拟接口回归；不启动服务、不联网、不调用模型。"""
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'maibot'))
sys.path.insert(0,str(ROOT/'tests'))
suite=unittest.TestSuite()
for name in ('test_compact','test_engine_regression','test_route2','test_adapter_regression','test_compatibility','test_increment_install'):
    suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(__import__(name)))
result=unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(not result.wasSuccessful())
