"""为固定版本安装 Heart 接口和前端面板；不会改动 MaiBot 原生代码。"""
from pathlib import Path


def apply(root):
    server = root / 'src/open_llm_vtuber/server.py'
    index = root / 'frontend/index.html'
    source = server.read_text(encoding='utf-8')
    html = index.read_text(encoding='utf-8')
    anchor = '        self.app = FastAPI(title="Open-LLM-VTuber Server")  # Added title for clarity'
    addition = '\n        from heart_bridge import create_router\n        self.app.include_router(create_router())'
    tag = '<script type="module" src="./heart-avatar.mjs"></script>'
    if addition not in source:
        if source.count(anchor) != 1:
            raise RuntimeError('后端版本不匹配，未安装')
        source = source.replace(anchor, anchor + addition)
    if tag not in html:
        if html.count('</body>') != 1:
            raise RuntimeError('前端入口不匹配，未安装')
        html = html.replace('</body>', tag + '\n</body>')
    for relative in ('heart_bridge.py','frontend/heart-avatar.mjs','frontend/heart-controller.mjs'):
        if not (root / relative).is_file():
            raise RuntimeError('缺少文件：' + relative)
    server.write_text(source,encoding='utf-8')
    index.write_text(html,encoding='utf-8')
    print('PASS: Heart bridge and persistent mood panel installed')


if __name__ == '__main__':
    apply(Path(__file__).resolve().parents[1])
