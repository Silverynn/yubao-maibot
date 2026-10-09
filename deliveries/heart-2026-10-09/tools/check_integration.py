"""只读检查两侧接入条件；通过不等同于真实模型或麦克风验收。"""
from pathlib import Path

import argparse
import json
import re

parser=argparse.ArgumentParser();parser.add_argument('--maibot-root',required=True)
parser.add_argument('--vt-root',required=True);parser.add_argument('--plugin-dir')
args=parser.parse_args();mai=Path(args.maibot_root).resolve();vt=Path(args.vt_root).resolve()
plugin=Path(args.plugin_dir).resolve() if args.plugin_dir else mai/'plugins/heart-realtime'
checks=[]
def check(root,relative,signals=()):
    path=root/relative
    if not path.is_file():checks.append({'file':str(relative),'ready':False,'error':'缺少文件'});return
    text=path.read_text(encoding='utf-8')
    missing=[signal for signal in signals if signal not in text]
    checks.append({'file':str(relative),'ready':not missing,'missing_contracts':missing})
def version(root,expected):
    path=root/'pyproject.toml'
    text=path.read_text(encoding='utf-8') if path.is_file() else ''
    found=re.search(r'^version\s*=\s*"([^"]+)"',text,re.M)
    actual=found.group(1) if found else None
    checks.append({'project':root.name,'version':actual,'ready':actual==expected,'tested_version':expected})
version(mai,'1.2.5');version(vt,'1.2.1')
for relative,signals in {
    'heart_shared/storage.py':['class AuditStore'],
    'heart_shared/card_writer.py':['def start_writer'],
    'src/services/heart_memory_backend.py':['class NativeBackend'],
    'src/services/heart_memory_scope.py':[],
    'src/services/heart_live2d_delivery.py':['skip_live2d_typing'],
    'src/services/heart_realtime.py':["'api_version':1",'character_names'],
    'src/services/heart_realtime_drafts.py':['async def generate_reply'],
    'src/plugin_runtime/capabilities/registry.py':['_register("heart.realtime", realtime_capability)'],
    'src/services/send_service.py':['can_deliver(message)','defer_delivery(message)'],
    'src/chat/replyer/maisaka_generator_base.py':['src.services.heart_realtime_drafts','await generate_reply('],
}.items():check(mai,relative,signals)
for relative,signals in {
    'heart_realtime_client.py':['HEART_REALTIME_PLUGIN_DIR','async def final_input'],
    'heart_bridge.py':['model_provider','expression_catalog'],
    'expression_catalog.py':['model_expression_names'],
    'voice_context.py':['client_uid','requested_voice'],
    'dialogue_trace.py':['current_turn'],
    'src/open_llm_vtuber/websocket_handler.py':['heart-rt','heart_realtime_client'],
    'src/open_llm_vtuber/conversations/conversation_handler.py':['heart_rt_utterance'],
    'src/open_llm_vtuber/conversations/single_conversation.py':['observe_turn_mode','final_input'],
    'src/open_llm_vtuber/conversations/tts_manager.py':['annotate_payload','eligible_payload'],
    'src/open_llm_vtuber/agent/agents/maibot_agent.py':['heart_realtime_client'],
    'maibot_client.py':['bind_native','received_draft','prepare_correction'],
    'frontend/index.html':['heart-realtime.mjs'],
    'frontend/heart-realtime.mjs':['在你说话时就开始生成回答，加快回答速度但可能增加模型用量'],
}.items():check(vt,relative,signals)
mainfiles=list((vt/'frontend/assets').glob('main-*.js'))
if len(mainfiles)==1:
    check(vt,mainfiles[0].relative_to(vt),['heartRealtime?.frame','heartRealtime?.begin',
        'heartRealtime?.attach','heartRealtime?.acceptTask','heartRealtime?.textShown'])
else:checks.append({'file':'frontend/assets/main-*.js','ready':False,'error':'构建文件数量/名称变化，需要重新适配VAD和播放队列'})
for relative in ('plugin.py','engine.py','speculation.py','runtime_config.py','stream_asr.py','_manifest.json'):
    check(plugin,relative)
print(json.dumps({'ready':all(c['ready'] for c in checks),'scope':__doc__,'checks':checks},ensure_ascii=False,indent=2))
raise SystemExit(0 if all(c['ready'] for c in checks) else 1)
