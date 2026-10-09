"""已构建前端的可重复定点补丁；不匹配即停止，不猜测混淆变量。"""
from pathlib import Path
import sys


def patch(source):
    replacements = [
        ('console.error("Audio play error:",Rt),Pt()',
         'console.error("Audio play error:",Rt),window.dispatchEvent(new CustomEvent("heart-playback",{detail:{phase:"play_failed",trace:rt.heartTrace}})),Pt()'),
        ('console.error("Audio playback error:",Rt),Pt()',
         'console.error("Audio playback error:",Rt),window.dispatchEvent(new CustomEvent("heart-playback",{detail:{phase:"play_failed",trace:rt.heartTrace}})),Pt()'),
        ('appendHumanMessage:lt,appendOrUpdateToolCallMessage:ut}=useChatHistory()',
         'appendHumanMessage:lt,appendOrUpdateToolCallMessage:ut,appendAIMessage:heartAppendAI,appendResponse:heartAppendResponse}=useChatHistory()'),
        ('case"full-text":Gt.text&&rt(Gt.text);break;',
         'case"heart-turn-start":break;case"heart-text-preview":if(st!=="interrupted"&&st!=="listening"&&(window.heartDialogue?.acceptPreview(Gt.heart_trace)??true)){const hp=Gt.display_text;hp&&(heartAppendResponse(hp.text),heartAppendAI(hp.text,hp.name,hp.avatar),rt(hp.text),window.heartDialogue?.textShown(Gt.heart_trace))}break;case"full-text":Gt.text&&rt(Gt.text);break;'),
        ('forwarded:Gt.forwarded||!1', 'forwarded:Gt.forwarded||!1,previewed:Gt.heart_previewed||false,heartTrace:Gt.heart_trace||null'),
        ('ft&&(ut(ft.text),ct(ft.text,ft.name,ft.avatar),lt(ft.text),',
         'ft&&(rt.previewed||(ut(ft.text),ct(ft.text,ft.name,ft.avatar)),lt(ft.text),'),
        ('kt.addEventListener("playing",()=>window.dispatchEvent(new CustomEvent("heart-speech",{detail:"playing"})))',
         'kt.addEventListener("playing",()=>{window.dispatchEvent(new CustomEvent("heart-playback",{detail:{phase:"playing",trace:rt.heartTrace}}));window.dispatchEvent(new CustomEvent("heart-speech",{detail:"playing"}))})'),
        ('It=!0,window.dispatchEvent(new CustomEvent("heart-speech",{detail:"ended"})),ot()',
         'It=!0,window.dispatchEvent(new CustomEvent("heart-playback",{detail:{phase:"ended",trace:rt.heartTrace}})),window.dispatchEvent(new CustomEvent("heart-speech",{detail:"ended"})),ot()'),
        ('kt.addEventListener("pause",()=>window.dispatchEvent(new CustomEvent("heart-speech",{detail:"ended"})))',
         'kt.addEventListener("pause",()=>{window.dispatchEvent(new CustomEvent("heart-playback",{detail:{phase:"ended",trace:rt.heartTrace}}));window.dispatchEvent(new CustomEvent("heart-speech",{detail:"ended"}))})'),
    ]
    for old,new in replacements:
        if source.count(new)==1:
            continue
        if source.count(old)!=1:
            raise RuntimeError('前端锚点不符；请检查版本，禁止继续安装：'+old[:70])
        source=source.replace(old,new,1)
    return source


if __name__=='__main__':
    path=Path(sys.argv[1]); original=path.read_text(encoding='utf-8'); updated=patch(original)
    if updated!=original: path.write_text(updated,encoding='utf-8')
    print('PASS：文字提前显示；音频不重复追加文字；播放绑定本轮交互')
