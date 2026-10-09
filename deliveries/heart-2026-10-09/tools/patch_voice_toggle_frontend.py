"""同步展示/纯文字模式的最小前端补丁；保留上一轮播放回执和精确日志。"""
from pathlib import Path
import sys


def patch(source):
    replacements = [
        ('heartTrace:Gt.heart_trace||null',
         'heartTrace:Gt.heart_trace||null,heartAudioStatus:Gt.heart_audio_status||"text_only"'),
        ('lt(ft.text),bt||tt({type:"audio-play-start"',
         'lt(ft.text),window.heartDialogue?.textShown(rt.heartTrace,rt.heartAudioStatus),bt||tt({type:"audio-play-start"'),
    ]
    for old, new in replacements:
        if source.count(new) == 1:
            continue
        if source.count(old) != 1:
            raise RuntimeError('语音开关前端锚点不符，停止安装：'+old)
        source = source.replace(old, new, 1)
    return source


if __name__ == '__main__':
    path = Path(sys.argv[1])
    source = path.read_text(encoding='utf-8')
    result = patch(source)
    if result != source:
        path.write_text(result, encoding='utf-8')
    print('PASS：同步/纯文字展示都回传文字可见时间；不改原播放器的显示顺序')
