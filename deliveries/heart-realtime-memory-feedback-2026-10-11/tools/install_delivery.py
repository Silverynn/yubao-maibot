"""默认只读检查；仅部署白名单源码，未知改动整批拒绝。"""
from pathlib import Path
from datetime import datetime
import argparse,hashlib,json,shutil

ROOT=Path(__file__).resolve().parents[1]

def digest(path):
    return hashlib.sha256(path.read_bytes().decode('utf-8-sig').replace('\r\n','\n').encode()).hexdigest() if path.is_file() else None

def inside(root,relative):
    p=Path(relative)
    if p.is_absolute() or '..' in p.parts:raise ValueError('目录外路径被拒绝')
    target=(root/p).resolve()
    if not target.is_relative_to(root.resolve()):raise ValueError('目录外链接被拒绝')
    return target

def plugin_folder(root,plugin_id,default,required=False):
    matches=[]
    for folder in (root/'plugins').iterdir():
        manifest=folder/'_manifest.json'
        if manifest.is_file():
            data=json.loads(manifest.read_text(encoding='utf-8-sig'))
            if data.get('id')==plugin_id:matches.append(folder.relative_to(root).as_posix())
    if len(matches)>1:raise ValueError('插件重复：'+plugin_id)
    if required and not matches:raise ValueError('先安装 PR #5 的实时插件基础包')
    return matches[0] if matches else default

def plan(root,package=ROOT):
    root=root.resolve()
    if not (root/'src/services/heart_realtime_drafts.py').is_file() or not (root/'src/services/heart_memory_backend.py').is_file():
        raise ValueError('目标缺少 PR #5 所需宿主接口，不能只安装本增量')
    rt=plugin_folder(root,'heart.realtime','plugins/heart-realtime',True)
    weather=plugin_folder(root,'heart.weather','plugins/heart_weather')
    manifest=json.loads((package/'INSTALL_MANIFEST.json').read_text(encoding='utf-8'))
    rows=[]
    for entry in manifest['entries']:
        name=entry['target']
        for prefix,folder in [('plugins/heart-realtime',rt),('plugins/heart_weather',weather)]:
            if name.startswith(prefix+'/'):name=folder+name[len(prefix):]
        source=inside(package,entry['path']);target=inside(root,name)
        if digest(source)!=entry['sha256']:raise ValueError('包内校验失败：'+entry['path'])
        if target.exists() and not target.is_file():raise ValueError('目标不是普通文件：'+name)
        current=digest(target)
        if current==entry['sha256']:state='same'
        elif current in entry['accepted_before']:state='new' if current is None else 'update'
        else:state='conflict'
        rows.append({**entry,'resolved_target':name,'current':current,'state':state})
    return rows

def apply(root,rows,*,stopped=False,package=ROOT):
    if not stopped:raise ValueError('先停止 MaiBot 与守护程序，再使用 --stopped')
    if any(row['state']=='conflict' for row in rows):raise ValueError('未知自改版：整批不写入；按 baseline 与 patches 三方合并')
    root=root.resolve()
    # 防止只读检查与安装间目标发生变化；所有目标先核验。
    for row in rows:
        if digest(inside(root,row['resolved_target']))!=row['current']:raise ValueError('预检后文件已变化')
        if digest(inside(package,row['path']))!=row['sha256']:raise ValueError('预检后发布包已变化')
    backup=inside(root, 'data/heart_install_backups/'+datetime.now().strftime('memory-feedback-%Y%m%d-%H%M%S-%f'))
    changed=[row for row in rows if row['state']!='same']
    if not changed:return {'changed':0}
    backup.mkdir(parents=True)
    # 全量备份完成后才写第一份源码；配置、日志、记忆数据库不在部署清单中。
    for row in changed:
        old=inside(root,row['resolved_target']);saved=inside(backup,row['resolved_target'])
        if old.exists():saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(old,saved)
    (backup/'receipt.json').write_text(json.dumps(changed,ensure_ascii=False,indent=2),encoding='utf-8')
    for row in changed:
        target=inside(root,row['resolved_target']);target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_suffix(target.suffix+'.feedback-install.tmp')
        if temporary.is_symlink():raise ValueError('拒绝安装临时文件链接')
        temporary.write_bytes(inside(package,row['path']).read_bytes());temporary.replace(target)
    return {'changed':len(changed),'backup':str(backup)}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--maibot-root',type=Path,required=True)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--stopped',action='store_true')
    args=parser.parse_args();rows=plan(args.maibot_root)
    print(json.dumps([{'target':r['resolved_target'],'state':r['state']} for r in rows],ensure_ascii=False,indent=2))
    if any(r['state']=='conflict' for r in rows):print('存在冲突；整批未写入。');return 2
    if args.apply:print(json.dumps(apply(args.maibot_root,rows,stopped=args.stopped),ensure_ascii=False,indent=2))
    else:print('仅比较，未写入。确认停服务后，加 --apply --stopped 安装。')
    return 0

if __name__=='__main__':raise SystemExit(main())
