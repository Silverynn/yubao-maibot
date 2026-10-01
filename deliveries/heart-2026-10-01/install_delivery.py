"""默认只做比较，不安装；未知文件不覆盖，心情与原生改动必须分别选择。"""
from pathlib import Path
from datetime import datetime
import argparse
import hashlib
import json
import shutil

ROOT=Path(__file__).resolve().parent

def digest(path):
 return hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()

def safe_target(root, relative):
 target=(root/relative).resolve()
 if not target.is_relative_to(root.resolve()):raise ValueError('目标路径越出机器人目录')
 return target

def plan(target, include_mood=False, include_native=False, include_dashboard=False):
 manifest=json.loads((ROOT/'INSTALL_MANIFEST.json').read_text(encoding='utf-8'))
 selected={'shared','memory','adapter'}
 if include_mood:selected.add('mood')
 if include_native:selected.add('native')
 if include_dashboard:selected.add('dashboard')
 result=[]
 for entry in manifest['entries']:
  if entry['component'] not in selected:continue
  dest=safe_target(target,entry['target']);source=ROOT/entry['path']
  if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=entry['sha256']:
   raise ValueError('交付文件校验失败：'+entry['path'])
  current=digest(dest) if dest.is_file() else None
  expected={entry['normalized_sha256'],entry.get('previous_normalized_sha256'),entry.get('upstream_normalized_sha256')}-{None}
  state='相同，跳过' if current==entry['normalized_sha256'] else '新文件' if current is None else '可更新：匹配已知版本' if current in expected else '本地改动/未知版本，必须人工合并'
  # 原生旧版本若无精确指纹匹配，不能以“旧插件包”推测宿主源码。
  result.append(dict(entry,state=state,current=current))
 return result

def apply(target, rows, stopped=False, ack_shared_merge=False):
 if not stopped:raise ValueError('请先关闭 MaiBot/WebUI，确认后提供 --stopped')
 if not ack_shared_merge:raise ValueError('共享库也影响心情插件；对方GPT审查合并后才提供 --ack-shared-merge')
 if any('人工合并' in row['state'] for row in rows):raise ValueError('存在未知本地修改，未安装任何文件；请逐文件人工合并')
 # 无写入前先完成全部检查；每个可回滚旧文件独立备份，绝不复制或清理真实记忆。
 backup=target/'.runtime'/('heart-delivery-20261001-backup-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
 backup.mkdir(parents=True,exist_ok=False)
 changed=[]
 for row in rows:
  if row['state'].startswith('相同'):continue
  dest=safe_target(target,row['target']);old=backup/row['target']
  if dest.exists():old.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,old)
  changed.append({'target':row['target'],'had_previous':dest.exists(),'after_sha256':row['sha256']})
 (backup/'ROLLBACK.json').write_text(json.dumps(changed,ensure_ascii=False,indent=2),encoding='utf-8')
 for row in rows:
  if row['state'].startswith('相同'):continue
  dest=safe_target(target,row['target']);dest.parent.mkdir(parents=True,exist_ok=True)
  shutil.copy2(ROOT/row['path'],dest)
 print('安装完成，代码备份：',backup)
 print('未修改 config.toml、模型配置、机器人身份、数据库或Live2D；前端另需在 dashboard 中构建。')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--target',required=True,type=Path)
 p.add_argument('--include-mood',action='store_true');p.add_argument('--include-native',action='store_true')
 p.add_argument('--include-dashboard',action='store_true');p.add_argument('--apply',action='store_true')
 p.add_argument('--stopped',action='store_true');p.add_argument('--ack-shared-merge',action='store_true')
 a=p.parse_args()
 if not (a.target/'src').is_dir():p.error('target 必须是MaiBot源码根目录，不是整个合作仓库或System32')
 rows=plan(a.target,a.include_mood,a.include_native,a.include_dashboard)
 print(json.dumps(rows,ensure_ascii=False,indent=2))
 if a.apply:apply(a.target,rows,a.stopped,a.ack_shared_merge)
 else:print('只读预检结束，没有安装；默认未选心情、原生和前端。Live2D永不自动安装。')
