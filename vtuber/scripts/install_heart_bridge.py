"""为固定版本安装 Heart 接口和前端面板；不会改动 MaiBot 原生代码。"""
from pathlib import Path


def apply(root):
    server = root / 'src/open_llm_vtuber/server.py'
    websocket_handler = root / 'src/open_llm_vtuber/websocket_handler.py'
    single_conversation = root / 'src/open_llm_vtuber/conversations/single_conversation.py'
    index = root / 'frontend/index.html'
    source = server.read_text(encoding='utf-8')
    websocket_source = websocket_handler.read_text(encoding='utf-8')
    conversation_source = single_conversation.read_text(encoding='utf-8')
    html = index.read_text(encoding='utf-8')
    anchor = '        self.app = FastAPI(title="Open-LLM-VTuber Server")  # Added title for clarity'
    old_addition = '\n        from heart_bridge import create_router\n        self.app.include_router(create_router())'
    cache_anchor = '        # It will be populated during the initialize method call'
    addition = ('\n        from heart_bridge import create_router\n'
                '        self.app.include_router(create_router(\n'
                '            model_provider=lambda: self.default_context_cache.live2d_model))')
    tag = '<script type="module" src="./heart-avatar.mjs"></script>'
    visit_anchor = '            await self._send_initial_messages(\n'
    visit_hook = '            from heart_bridge import begin_visit\n            begin_visit(client_uid)\n\n'
    metadata_anchor = '        batch_input = create_batch_input(\n'
    metadata_hook = '        metadata = {**(metadata or {}), "heart_client_uid": client_uid}\n'
    if old_addition in source:
        source = source.replace(old_addition, '')
    if addition not in source:
        if source.count(cache_anchor) != 1:
            raise RuntimeError('后端版本不匹配，未安装模型动态识别')
        source = source.replace(cache_anchor, cache_anchor + addition)
    if tag not in html:
        if html.count('</body>') != 1:
            raise RuntimeError('前端入口不匹配，未安装')
        html = html.replace('</body>', tag + '\n</body>')
    if visit_hook not in websocket_source:
        if websocket_source.count(visit_anchor) != 1:
            raise RuntimeError('WebSocket 版本不匹配，未安装会话隔离')
        websocket_source = websocket_source.replace(visit_anchor, visit_hook + visit_anchor)
    if metadata_hook not in conversation_source:
        if conversation_source.count(metadata_anchor) != 1:
            raise RuntimeError('对话流程版本不匹配，未安装会话隔离')
        conversation_source = conversation_source.replace(metadata_anchor, metadata_hook + metadata_anchor)
    for relative in ('heart_bridge.py','expression_catalog.py','frontend/heart-avatar.mjs','frontend/heart-controller.mjs'):
        if not (root / relative).is_file():
            raise RuntimeError('缺少文件：' + relative)
    server.write_text(source,encoding='utf-8')
    websocket_handler.write_text(websocket_source,encoding='utf-8')
    single_conversation.write_text(conversation_source,encoding='utf-8')
    index.write_text(html,encoding='utf-8')
    print('PASS: Heart bridge, per-visit chat isolation and mood panel installed')


if __name__ == '__main__':
    apply(Path(__file__).resolve().parents[1])
