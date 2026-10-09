"""为已匹配的前端构建添加VAD/播放接缝，默认只检查；不会覆盖整份JS。"""
from pathlib import Path

import argparse
import datetime
import json
import shutil

parser=argparse.ArgumentParser();parser.add_argument('--vt-root',required=True)
parser.add_argument('--apply',action='store_true');args=parser.parse_args()
vt=Path(args.vt_root).resolve();files=list((vt/'frontend/assets').glob('main-*.js'))
if len(files)!=1:raise SystemExit('主构建文件数量发生变化，请重新适配；没有修改文件')
path=files[0].resolve()
if not path.is_relative_to(vt):raise SystemExit('构建文件不在指定VT目录内，停止')
pairs=json.loads((Path(__file__).resolve().parent/'frontend-hooks.json').read_text(encoding='utf-8'))
original=path.read_text(encoding='utf-8');updated=original
for before,after in pairs:
    if updated.count(after)==1:continue
    if updated.count(before)!=1:raise SystemExit('接缝不匹配，请手工适配；没有修改文件：'+before[:70])
    updated=updated.replace(before,after,1)
if args.apply and updated!=original:
    backup=path.with_name(path.name+'.before-realtime-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(path,backup);path.write_text(updated,encoding='utf-8')
    print('已备份并添加接缝：'+str(backup))
else:print('接缝检查通过；'+('已存在，无须修改' if updated==original else '未修改，可加--apply应用'))
