"""验证发布包完整性，禁止真实配置、聊天、缓存和模型资产混入。"""
from pathlib import Path
import argparse
import hashlib
import json

def verify(root):
 hashes=json.loads((root/'SHA256.json').read_text(encoding='utf-8'))
 actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and '.runtime' not in p.parts}
 if actual!=set(hashes)|{'SHA256.json'}:raise ValueError('文件清单不匹配')
 for name,value in hashes.items():
  p=(root/name).resolve()
  if not p.is_relative_to(root.resolve()):raise ValueError('路径越界')
  if hashlib.sha256(p.read_bytes()).hexdigest()!=value:raise ValueError('文件变化：'+name)
  if p.name in {'config.toml','model_config.toml','bot_config.toml','conf.yaml'} or p.suffix in {'.db','.sqlite3','.log','.bin','.moc3','.png','.jpg'}:
   raise ValueError('禁止交付真实配置/运行数据/形象资产：'+name)
 print('PASS：',len(hashes),'个代码/文档/差异文件校验通过；不包含真实配置和运行数据')
 return len(hashes)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
 verify(p.parse_args().root)
