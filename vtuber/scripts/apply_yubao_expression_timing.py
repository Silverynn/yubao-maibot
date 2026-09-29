"""修复指定前端版本的无声表情，并安装短暂表情恢复；可安全重复执行。"""
import re
from pathlib import Path


def patch_bundle(original):
    """先校验全部锚点，再返回新内容；版本不匹配时不写入半份补丁。"""
    old = 'setExpression(o){var s;(s=this.getModel())==null||s.setExpression(o)}'
    new = 'setExpression(o){yubaoSetTimedExpression(this,o)}'
    imported_old = 'import{yubaoSetTimedExpression}from"../yubao-expression-timing.mjs";\n'
    imported = 'import{yubaoSetTimedExpression,yubaoShouldDeferReset}from"../yubao-expression-timing.mjs";\n'
    if original.startswith(imported_old):
        original = imported + original[len(imported_old):]
    if not (original.count(new) == 1 and original.startswith(imported)):
        if original.count(old) != 1 or new in original or imported in original:
            raise RuntimeError("Frontend adapter changed; inspect before applying the timing hook")
        original = imported + original.replace(old, new, 1)

    reset_old = 'o=reactExports.useCallback((s,a)=>{if(s)try{const _=s.getModel();'
    reset_new = 'o=reactExports.useCallback((s,a)=>{if(yubaoShouldDeferReset(s))return;if(s)try{const _=s.getModel();'
    if original.count(reset_new) != 1:
        if original.count(reset_old) != 1:
            raise RuntimeError("Frontend expression reset changed; inspect before patching")
        original = original.replace(reset_old, reset_new, 1)

    # 无声回复也应更新字幕，不能一直停在 Thinking...。
    subtitle_old = 'ft&&(ut(ft.text),ct(ft.text,ft.name,ft.avatar),dt&&lt(ft.text),bt||'
    subtitle_new = 'ft&&(ut(ft.text),ct(ft.text,ft.name,ft.avatar),lt(ft.text),bt||'
    if original.count(subtitle_new) != 1:
        if original.count(subtitle_old) != 1:
            raise RuntimeError("Frontend subtitle handler changed; inspect before patching")
        original = original.replace(subtitle_old, subtitle_new, 1)

    # 原前端在 if(dt)（有声音）里面换表情。把表情执行移到分支外，
    # 让 audio=null 的文字回复也能换表情，且有声回复仍只执行一次。
    audio_gate = 'try{if(dt){const gt=`data:audio/wav;base64,${dt}`'
    expression = ('const Ct=(mt=window.getLAppAdapter)==null?void 0:mt.call(window);'
                  'Ct&&(ht==null?void 0:ht[0])!==void 0&&st(ht[0],Ct,`Set expression to: ${ht[0]}`)')
    fixed_gate = 'try{' + expression + ';if(dt){const gt=`data:audio/wav;base64,${dt}`'
    if original.count(fixed_gate) == 1 and expression + ',' not in original:
        return original
    if original.count(audio_gate) != 1 or original.count(expression + ',') != 1:
        raise RuntimeError("Frontend audio handler changed; inspect before applying silent-expression fix")
    return original.replace(expression + ',', '', 1).replace(audio_gate, fixed_gate, 1)


def apply(root):
    frontend = root / "frontend"
    index = (frontend / "index.html").read_text(encoding="utf-8")
    matches = re.findall(r'src="\./(assets/main-[^"]+\.js)"', index)
    if len(matches) != 1:
        raise RuntimeError("Cannot identify the current frontend entry bundle")
    if not (frontend / "yubao-expression-timing.mjs").is_file():
        raise RuntimeError("Missing expression timing module")
    bundle = frontend / matches[0]
    original = bundle.read_text(encoding="utf-8")
    updated = patch_bundle(original)
    if updated != original:
        bundle.write_text(updated, encoding="utf-8")
    print("PASS: silent expressions and timing hook installed")


if __name__ == "__main__":
    apply(Path(__file__).resolve().parents[1])
