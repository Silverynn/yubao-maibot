"""Apply the small timing hook to the installed frontend build; safe to rerun."""
import re
from pathlib import Path


def apply(root):
    frontend = root / "frontend"
    index = (frontend / "index.html").read_text(encoding="utf-8")
    matches = re.findall(r'src="\./(assets/main-[^"]+\.js)"', index)
    if len(matches) != 1:
        raise RuntimeError("Cannot identify the current frontend entry bundle")
    bundle = frontend / matches[0]
    original = bundle.read_text(encoding="utf-8")
    old = 'setExpression(o){var s;(s=this.getModel())==null||s.setExpression(o)}'
    new = 'setExpression(o){yubaoSetTimedExpression(this,o)}'
    imported = 'import{yubaoSetTimedExpression}from"../yubao-expression-timing.mjs";\n'
    if original.count(new) == 1 and original.startswith(imported):
        print("Expression timing hook already installed")
        return
    if original.count(old) != 1 or new in original or imported in original:
        raise RuntimeError("Frontend adapter changed; inspect before applying the timing hook")
    if not (frontend / "yubao-expression-timing.mjs").is_file():
        raise RuntimeError("Missing expression timing module")
    updated = imported + original.replace(old, new, 1)
    bundle.write_text(updated, encoding="utf-8")
    print("Expression timing hook installed")


if __name__ == "__main__":
    apply(Path(__file__).resolve().parents[1])
