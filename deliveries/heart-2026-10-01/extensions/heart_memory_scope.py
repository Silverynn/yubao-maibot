"""MaiBot 接入层隐私边界：不改 A_memorix 算法，不修改原生数据库。"""
import time

from heart_shared.memory_scope import visible, metadata, filter_result


def session_for(args):
    from src.chat.message_receive.chat_manager import chat_manager
    chat = str(args.get('chat_id') or args.get('session_id') or '')
    return chat_manager.get_existing_session_by_session_id(chat) if chat else None


async def kernel_for_read():
    from src.services.memory_service import a_memorix_host_service
    return await a_memorix_host_service._ensure_kernel()


async def scoped_paragraphs(chat_id, person_id='', limit=20):
    """列表/画像走原生来源索引，不额外调用模型或依赖语义相似度阈值。"""
    kernel = await kernel_for_read()
    store = kernel.metadata_store
    source = f'person_fact:{person_id}' if person_id else f'chat_summary:{chat_id}'
    rows = store.get_paragraphs_by_source(source)
    hits = [dict(row, type='paragraph', score=1.0, metadata=metadata(row)) for row in rows
            if visible(row, chat_id, person_id)]
    # 复用原生的有效性检查（例如已被替换的事实不能复活）。
    hits = kernel._get_search_hit_service()._filter_user_visible_hits(hits)
    ordered = sorted(hits, key=lambda h: float(h.get('created_at') or 0), reverse=True)
    return ordered if limit is None else ordered[:limit]


async def scoped_group_person_facts(chat_id, limit=20):
    """浏览本群全部人物的事实；只按可核实的会话来源放行。"""
    kernel = await kernel_for_read()
    store = kernel.metadata_store
    rows = store.query("""
        SELECT * FROM paragraphs
        WHERE source GLOB 'person_fact:*'
          AND (is_deleted IS NULL OR is_deleted = 0)
          AND CASE WHEN json_valid(metadata) THEN (
              json_extract(metadata, '$.chat_id') = ?
              OR EXISTS (
                  SELECT 1 FROM json_each(metadata, '$.chat_ids')
                  WHERE CAST(value AS TEXT) = ?
              )
          ) ELSE 0 END
        ORDER BY created_at DESC
    """, (chat_id, chat_id))
    hits = [dict(row, type='paragraph', score=1.0, metadata=metadata(row)) for row in rows
            if visible(row, chat_id)]
    hits = kernel._get_search_hit_service()._filter_user_visible_hits(hits)
    return sorted(hits, key=lambda h: float(h.get('created_at') or 0), reverse=True)[:limit]


async def scoped_profile(args):
    session = session_for(args)
    if session is None:
        return {'success': False, 'error': '无法核实画像调用会话，未读取全局画像'}
    person_id = str(args.get('person_id') or '')
    if not person_id and args.get('person_keyword'):
        from src.person_info.person_info import resolve_person_id_for_memory
        person_id = resolve_person_id_for_memory(person_name=str(args['person_keyword']))
    if not person_id:
        return {'success': True, 'profile_text': '', 'summary': '', 'hits': []}
    hits = await scoped_paragraphs(session.session_id, person_id, min(20, int(args.get('limit') or 8)))
    # 禁止使用跨私聊/群聊合成的全局画像快照。局部画像仅由本会话已存事实组成。
    text = '# 人物画像\n## 稳定了解\n' + '\n'.join('- '+h['content'] for h in hits) if hits else ''
    return {'success': True, 'person_id': person_id, 'profile_text': text, 'summary': text,
            'hits': hits, 'profile_source': '当前会话的原生长期事实', 'from_cache': False}


async def prepare_read(component, args):
    """仅聊天入口带 chat_id；管理员后台查询仍保留原版全局视图。"""
    chat = str(args.get('chat_id') or '')
    if component == 'search_memory' and chat:
        session = session_for(args)
        if session is None:
            return {'success': False, 'hits': [], 'error': '无法核实记忆检索会话'}
        if session.group_id:
            # 群聊从检索入口即限定当前群，不让“全局共享”设置先召回私聊再占满 top-k。
            from src.A_memorix.core.runtime.sdk_memory_kernel import KernelSearchRequest
            kernel = await kernel_for_read()
            return await kernel.search_memory(KernelSearchRequest(
                query=str(args.get('query') or ''), limit=max(1, int(args.get('limit') or 5)),
                mode=str(args.get('mode') or 'search'), chat_id=chat,
                shared_chat_ids=(), person_id=str(args.get('person_id') or ''),
                time_start=args.get('time_start'), time_end=args.get('time_end'),
                respect_filter=True, user_id=str(args.get('user_id') or ''),
                group_id=str(session.group_id)))
    if component in {'get_person_profile', 'memory_profile_admin'} and chat:
        if component == 'get_person_profile' or args.get('action') == 'query':
            return await scoped_profile(args)
    if component == 'heart_scoped_list':
        session = session_for(args)
        if not session or not session.group_id:
            return {'success': False, 'error': '无法核实当前群聊', 'hits': []}
        person_id = str(args.get('person_id') or '')
        limit = min(20, int(args.get('limit') or 5))
        hits = (await scoped_paragraphs(chat, person_id, limit) if person_id
                else await scoped_group_person_facts(chat, limit))
        # 先筛目标人物，再截取最近结果；否则仅看最新 limit 条摘要会漏掉稍早的相关内容。
        summaries = (await scoped_paragraphs(chat, '', None if person_id else limit)
                     if args.get('include_group_summaries', True) else [])
        name = str(args.get('target_name') or '').strip()
        keyword = str(args.get('query') or '').strip()
        for hit in summaries:
            # 只能说摘要“提到了此人”，不能把群讨论冒充此人明确说过的话。
            if name and name not in hit['content']:
                continue
            if keyword and keyword != name and keyword not in hit['content']:
                continue
            hits.append(dict(hit, group_context_only=bool(name)))
        hits = sorted(hits, key=lambda h: float(h.get('created_at') or 0), reverse=True)[:limit]
        return {'success': True, 'hits': hits, 'summary': '', 'scope_note': '当前群原生人物事实和群聊摘要'}
    return None


def constrain_search(args):
    session = session_for(args)
    if session is None:
        raise ValueError('无法核实记忆检索的真实会话；拒绝无范围检索')
    # 模型传来的 respect_filter=false 不能关闭隐私边界。
    return dict(args, chat_id=session.session_id, group_id=str(session.group_id or ''),
                respect_filter=True, shared_chat_ids=[])


async def stored_contents(component, args, result):
    """写入完成后按真实 ID/幂等来源核对正文；不把待写入文本冒充已存内容。"""
    if component not in {'ingest_text', 'ingest_summary'} or not isinstance(result, dict):
        return []
    if result.get('success') is False or result.get('error'):
        return []
    kernel = await kernel_for_read()
    store = kernel.metadata_store
    rows = [store.get_paragraph(str(key)) for key in result.get('stored_ids') or []]
    if component == 'ingest_summary' and args.get('external_id'):
        rows += [row for row in store.get_paragraphs_by_source('chat_summary:'+str(args.get('chat_id') or ''))
                 if metadata(row).get('external_id') == args['external_id']]
    return list(dict.fromkeys(str(row['content']) for row in rows if row and visible(row, str(args.get('chat_id') or ''))))
