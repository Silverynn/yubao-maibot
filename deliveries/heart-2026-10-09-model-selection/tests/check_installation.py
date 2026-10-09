"""用临时虚构目标验证10文件升级、幂等、冲突与失败回退，不启动服务。"""
from pathlib import Path
import importlib.util
import json
import subprocess
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('delivery_installer',ROOT/'tools/install_delivery.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
entries=json.loads((ROOT/'INSTALL_MANIFEST.json').read_text(encoding='utf-8'))['entries']
checks=[]
def ok(name,value):
    assert value,name
    checks.append(name)
with tempfile.TemporaryDirectory(prefix='heart-model-selection-check-') as scratch:
    base=Path(scratch)/'MaiBot';base.mkdir()
    for entry in entries:
        path=base/entry['target'];path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes((ROOT/entry['baseline']).read_bytes())
    sentinels={'config/model_config.toml':b'fictional existing model settings',
        'plugins/heart_mood/config.toml':b'fictional mood frequencies',
        'config/voice-local.txt':b'existing partner voice settings',
        'data/do-not-touch.txt':b'fictional retained memory data'}
    for name,data in sentinels.items():
        path=base/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    roots={'maibot':base};components={'model-selection'}
    plan=installer.plan(roots,components)
    ok('10个目标已知基线可升级',len(plan)==10 and all(r['state']=='update' for r in plan))
    for entry in entries:
        subprocess.run(['git','apply','--check',str(ROOT/entry['patch'])],cwd=base,check=True,capture_output=True)
    ok('10份增量补丁均可应用',True)
    try:installer.apply(roots,components,stopped=False,reviewed=True)
    except ValueError:ok('未停机不写入',True)
    else:raise AssertionError('缺少停机条件却写入')
    try:installer.apply(roots,components,stopped=True,reviewed=False)
    except ValueError:ok('未审阅不写入',True)
    else:raise AssertionError('缺少审阅条件却写入')
    first=base/entries[0]['target'];original=first.read_bytes()
    first.write_bytes(original+b'\n# partner modification\n')
    snapshot={entry['target']:(base/entry['target']).read_bytes() for entry in entries}
    try:installer.apply(roots,components,stopped=True,reviewed=True)
    except ValueError:ok('未知修改整批拒绝',all((base/name).read_bytes()==data for name,data in snapshot.items()))
    else:raise AssertionError('覆盖了伙伴改动')
    first.unlink()
    try:installer.apply(roots,components,stopped=True,reviewed=True)
    except ValueError:ok('缺失旧文件整批拒绝',not first.exists())
    else:raise AssertionError('缺失旧文件仍安装')
    first.write_bytes(original)
    saved_replace=installer.os.replace
    writes=0
    def fail_midway(*args):
        global writes
        writes+=1
        if writes==3:raise OSError('simulated write failure')
        return saved_replace(*args)
    with patch.object(installer.os,'replace',fail_midway):
        try:installer.apply(roots,components,stopped=True,reviewed=True)
        except OSError:ok('写入中失败全部回退',all((base/e['target']).read_bytes()==(ROOT/e['baseline']).read_bytes() for e in entries))
        else:raise AssertionError('未复现受控写入失败')
    ok('没有遗留临时写文件',not list(base.rglob('*.heart-delivery-tmp')))
    result=installer.apply(roots,components,stopped=True,reviewed=True)
    ok('正式演练升级10文件',result['changed']==10)
    ok('安装结果逐文件正确',all((base/e['target']).read_bytes()==(ROOT/e['path']).read_bytes() for e in entries))
    ok('模型语音频率记忆数据均保留',all((base/n).read_bytes()==data for n,data in sentinels.items()))
    ok('重复安装零修改',installer.apply(roots,components,stopped=True,reviewed=True)['changed']==0)
    for backup in result['backups'].values():
        backup=Path(backup)
        ok('10份旧源码备份齐全',all((backup/e['target']).read_bytes()==(ROOT/e['baseline']).read_bytes() for e in entries))
        ok('回退清单完整',len(json.loads((backup/'ROLLBACK.json').read_text(encoding='utf-8')))==10)
print(json.dumps({'passed':len(checks),'patches_checked':len(entries),'paid_api_calls':0,'bot_started':False},ensure_ascii=False))
