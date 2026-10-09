"""只修改匹配的VAD/播放接缝及模块导入；默认只读，不覆盖伙伴整份页面。"""
from pathlib import Path
from datetime import datetime
import argparse
import json
import re
from patch_dialogue_frontend import patch as dialogue_patch
from patch_voice_toggle_frontend import patch as voice_patch

ROOT=Path(__file__).resolve().parent

def prepare_js(source):
    # 已完成实时接入则不要把中间版接缝再次插入。
    if 'heartRealtime?.textShown' not in source:
        if 'heartDialogue?.textShown' not in source:
            source=dialogue_patch(source)
            source=voice_patch(source)
    for before,after in json.loads((ROOT/'frontend-hooks.json').read_text(encoding='utf-8')):
        if source.count(after)==1:continue
        if source.count(before)!=1:raise ValueError('构建锚点不匹配，未写入；需按接口手工移植：'+before[:70])
        source=source.replace(before,after,1)
    return source

def prepare_html(source):
    if len(re.findall(r'</head\s*>',source,re.I))!=1:raise ValueError('页面head边界不明确，未写入')
    additions=[]
    for name in ('heart-dialogue.mjs','heart-realtime.mjs','heart-avatar.mjs'):
        if not re.search(r'<script\b[^>]*\bsrc=[\"\'][^\"\']*'+re.escape(name)+r'(?:\?[^\"\']*)?[\"\']',source,re.I):
            additions.append(f'<script type="module" src="./{name}"></script>')
    if not additions:return source
    return re.sub(r'</head\s*>',lambda m:'\n'.join(additions)+'\n'+m.group(),source,count=1,flags=re.I)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vt-root',required=True,type=Path)
    p.add_argument('--apply',action='store_true');a=p.parse_args();root=a.vt_root.resolve()
    files=list((root/'frontend/assets').glob('main-*.js'))
    if len(files)!=1:raise ValueError('需唯一匹配前端构建；其他版本请移植接缝')
    paths=[files[0].resolve(),(root/'frontend/index.html').resolve()]
    if not all(x.is_relative_to(root) for x in paths):raise ValueError('拒绝目录外链接')
    before=[x.read_bytes() for x in paths]
    after=[prepare_js(before[0].decode('utf-8')),prepare_html(before[1].decode('utf-8'))]
    # 所有转换成功后才写入；不改用户语音配置和模型文件。
    changed=[(path,old,new.encode('utf-8')) for path,old,new in zip(paths,before,after) if old!=new.encode('utf-8')]
    if a.apply and changed:
        stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f');written=[]
        for path,old,_ in changed:path.with_name(path.name+'.before-heart-'+stamp).write_bytes(old)
        try:
            for path,old,new in changed:
                if path.read_bytes()!=old:raise ValueError('预检后文件变化，停止')
                path.write_bytes(new);written.append((path,old))
        except BaseException:
            for path,old in reversed(written):path.write_bytes(old)
            raise
    print(json.dumps({'ready':True,'changed_files':len(changed),'applied':a.apply},ensure_ascii=False))

if __name__=='__main__':main()
