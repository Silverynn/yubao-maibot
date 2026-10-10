"""默认只读比较。既有语音包、心情和配置不自动安装或替换。"""
from datetime import datetime
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_COMPONENTS={'shared','realtime','vt'}

def digest(data):
    return hashlib.sha256(data.decode('utf-8-sig').replace('\r\n','\n').encode('utf-8')).hexdigest()

def contained(root, relative):
    path=Path(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('拒绝目录外路径：'+relative)
    target=(root/path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError('拒绝目录外链接：'+relative)
    return target

def realtime_folder(root):
    matches=[]
    for folder in (root/'plugins').iterdir() if (root/'plugins').is_dir() else []:
        manifest=folder/'_manifest.json'
        if not manifest.is_file():continue
        try:info=json.loads(manifest.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):continue
        if info.get('id')=='heart.realtime':
            contained(root,folder.relative_to(root).as_posix())
            matches.append(folder)
    if len(matches)!=1:
        raise ValueError('增量要求已安装且只有一个 heart.realtime 插件；不能新建重复插件，请先安装基础交付或处理重复目录')
    return matches[0]

def plan(roots, components, package=ROOT):
    manifest=json.loads((package/'INSTALL_MANIFEST.json').read_text(encoding='utf-8'))
    known={e['component'] for e in manifest['entries']}
    if components-known:raise ValueError('未知组件：'+','.join(components-known))
    rows=[]
    for e in manifest['entries']:
        if e['component'] not in components:continue
        e=dict(e)
        if e['project'] not in roots:raise ValueError('缺少目标目录：'+e['project'])
        root=roots[e['project']].resolve()
        if not (root/'src').is_dir():raise ValueError('目标必须是实际源码根目录：'+str(root))
        prefix='plugins/heart-realtime/'
        if e['project']=='maibot' and e['target'].startswith(prefix):
            e['original_target']=e['target']
            e['target']=(realtime_folder(root).relative_to(root)/e['target'][len(prefix):]).as_posix()
        source=contained(package,e['path']);data=source.read_bytes()
        if hashlib.sha256(data).hexdigest()!=e['sha256']:raise ValueError('包内文件校验失败：'+e['path'])
        dest=contained(root,e['target'])
        if dest.exists() and not dest.is_file():raise ValueError('目标不是文件：'+e['target'])
        current=digest(dest.read_bytes()) if dest.is_file() else None
        if current==e['sha256']:state='same'
        elif current is None and e['before_sha256'] is None:state='new'
        elif current is not None and current==e['before_sha256']:state='update'
        else:state='conflict'
        rows.append(dict(e,state=state,current=current))
    return rows

def apply(roots, components, *, stopped=False, reviewed=False, package=ROOT):
    if not (stopped and reviewed):raise ValueError('必须先停服务并完成同伴自改版审查')
    manifest=json.loads((package/'INSTALL_MANIFEST.json').read_text(encoding='utf-8'))
    required=set(manifest.get('required_components', []))
    if required and components!=required:
        raise ValueError('本增量须同时更新 shared、realtime、vt，禁止半套安装')
    rows=plan(roots,components,package)
    if any(e['state']=='conflict' for e in rows):raise ValueError('发现缺失基线/未知改动，整批未写入；按baseline和patch三方合并')
    changed=[e for e in rows if e['state']!='same']
    if not changed:return {'changed':0}
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backups={}
    saved=[]
    # 所有源、目标、备份先验证；真实配置/数据不在清单中。
    for e in changed:
        root=roots[e['project']].resolve()
        dest=contained(root,e['target'])
        current=digest(dest.read_bytes()) if dest.exists() else None
        if current!=e['current']:raise ValueError('预检后文件变化，停止：'+e['target'])
        backup=contained(root,'.runtime/heart-realtime-increment-20261010-'+stamp)
        backup.mkdir(parents=True,exist_ok=True);backups[e['project']]=str(backup)
        old=dest.read_bytes() if dest.exists() else None
        if old is not None:
            p=contained(backup,e['target']);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(old)
        saved.append((e,dest,old))
    for project,backup in backups.items():
        records=[{'target':e['target'],'had_previous':old is not None,'after_sha256':e['sha256']}
                 for e,_,old in saved if e['project']==project]
        (Path(backup)/'ROLLBACK.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    written=[]
    try:
        for e,dest,old in saved:
            source=contained(package,e['path']);data=source.read_bytes()
            if hashlib.sha256(data).hexdigest()!=e['sha256']:raise ValueError('源文件发生变化')
            dest.parent.mkdir(parents=True,exist_ok=True)
            temp=dest.with_name(dest.name+'.heart-delivery-tmp')
            with temp.open('xb') as stream:stream.write(data)
            try:os.replace(temp,dest)
            finally:
                if temp.exists():temp.unlink()
            written.append((dest,old))
    except BaseException:
        for dest,old in reversed(written):
            if old is None:dest.unlink()
            else:dest.write_bytes(old)
        raise
    return {'changed':len(written),'backups':backups,'note':'未启动服务；未改语音选择、模型账号、人格或任何数据'}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--maibot-root',required=True,type=Path);p.add_argument('--vt-root',type=Path)
    p.add_argument('--components',default=','.join(sorted(DEFAULT_COMPONENTS)))
    p.add_argument('--apply',action='store_true');p.add_argument('--stopped',action='store_true')
    p.add_argument('--reviewed',action='store_true')
    a=p.parse_args();roots={'maibot':a.maibot_root}
    if a.vt_root:roots['vt']=a.vt_root
    components=set(a.components.split(','))
    rows=plan(roots,components)
    print(json.dumps([{'file':r['path'],'state':r['state']} for r in rows],ensure_ascii=False,indent=2))
    if a.apply:
        if not a.vt_root:raise ValueError('需要实际 VT 根目录，禁止只安装后端')
        if any(e['state']=='conflict' for e in rows):raise ValueError('发现自改代码，整批未写入；请三方合并')
        probe=subprocess.run([sys.executable,str(ROOT/'tools/check_integration.py'),
            '--maibot-root',str(a.maibot_root),'--vt-root',str(a.vt_root),
            '--plugin-dir',str(realtime_folder(a.maibot_root.resolve()))],capture_output=True,text=True,encoding='utf-8')
        if probe.returncode:
            print(probe.stdout);raise ValueError('现有宿主/前端接缝未满足基础交付要求，未写入')
        print(json.dumps(apply(roots,components,stopped=a.stopped,reviewed=a.reviewed),ensure_ascii=False,indent=2))
    else:print('仅比较，未写入；增量默认同时包含 shared、realtime、vt，不更改声线、模型配置或运行数据。')
    return 2 if any(e['state']=='conflict' for e in rows) else 0

if __name__=='__main__':raise SystemExit(main())
