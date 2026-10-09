"""宿主与插件共用事件格式；宿主直接落盘，避免跨进程通知超时丢日志。"""


def outcome(component, args, result, error):
    result = result if isinstance(result, dict) else {}
    if error or result.get('error'):
        return {'status': '调用失败', 'error': error or result['error'], 'submitted_text': args.get('text', '')}
    if component in {'search_memory', 'heart_scoped_list'}:
        return {'status': '检索失败' if result.get('success') is False else '检索完成',
                'query': args.get('query', ''), 'hits': result.get('hits') or [],
                'scope_note': result.get('scope_note', ''), 'scope_removed_count': result.get('scope_removed_count', 0)}
    if component in {'ingest_text', 'ingest_summary'}:
        detail = str(result.get('detail') or result.get('reason') or '')
        status = ('候选待确认' if '候选' in detail else '冲突待确认') if result.get('pending') else (
            '写入失败' if result.get('success') is False else '原生写入结果')
        return {'status': status, 'detail': detail, 'submitted_text': args.get('text', ''),
                'stored_ids': result.get('stored_ids') or [], 'skipped_ids': result.get('skipped_ids') or [],
                'stored_contents': result.get('_heart_stored_contents') or []}
    return {'status': '服务返回', 'result': result}


def record(store, event):
    args = event.get('arguments') or {}
    meta = args.get('metadata') or {}
    ids = args.get('_heart_source_message_ids') or meta.get('evidence_message_ids') or []
    store.append('记忆操作结果', str(args.get('chat_id') or args.get('session_id') or ''),
                 event_id=event['event_id'], operation=event['component'], evidence_message_ids=ids,
                 duration_ms=event['duration_ms'], source_type=args.get('source_type', ''),
                 person_ids=args.get('person_ids') or [], person_id=args.get('person_id', ''),
                 action=args.get('action', ''), outcome=outcome(event['component'], args, event.get('result'), event.get('error')))
