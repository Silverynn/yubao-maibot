"""本轮全库维护；固定人工核对范围、原生纠错、有完整备份、不调用外部模型。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import asyncio
import json
import sqlite3

from heart_shared.memory_scope import metadata, visible
from heart_shared.storage import AuditStore
from heart_shared.summary_novelty import save_cursor
from src.A_memorix.host_service import a_memorix_host_service
from src.A_memorix.core.runtime.sdk_memory_kernel import SDKMemoryKernel
from src.A_memorix.core.utils.episode_service import EpisodeService
from src.A_memorix.core.utils.hash import compute_paragraph_hash

# 已逐条核对的无事实摘要；不以通用关键词匹配自动删除其他内容。
EMPTY = set('''62be0c8340820ad75ee461bf58ad5c21ed6153f69cfa5b55235eaf81ee30572e
5b575e5f566daa3cb583846f30302d98c349f737a698d6eaeb518cc05b79c0c1
42051f1aa0ba1f46fd2a1dcb71f06398037ba945149b973b04d2a56fabe32fb2
bbbdef9c8ced8edfd4db69a768e90890d9eeb532d09d76d4b13f0aebd86f8426
701ed8e5c71d2d8c272b94327937b3eb610dbff6afa487be908e12b4834b3917
ff7d3eeead1835e04177d61db2e5dc4577656c9e5ea83eaab2565bf8785422b0
87fa77a5e5ea08a1d24808c35d5c9697baea0e0d4c1cc62954e14b8f9d0d2055
fbcf01b64ba5afe5804326bed367e4df1455147079eb0f2a858c8299c2dbb90b
453046480220aea357de82cc483bb08d3ae21d0889a199a93908b29aa4940024
b1a24d576d4757932344e22a58264c01feb799898eefb4f4e8c0f1930d136da2
2c650a757e578e4521f27d8ec4c8634a935070f1a0c042e92a528e1757aff15c
d4003a5873da9cc8b5cba045e5dfefe9f7443b506213903959c5cff97a58ddeb
7d81db72825ce9b20a429c7173b35e1916fcd3c0fe0b2ead1bf103e047e78926
ce7450fe5e026b01daae460fe5fe09af8bdb9aeca84f3e5fcf6e1f569c9fe3d7
639278dcf05646d9da5be714c35c2a26c33b894ee0830d4ef0483c014d0c404a'''.split())
GROUP_KEEP = 'c9720746896d2c1ca85f5ee0c2d920acfe9c0c1331b7365e0fe91913ef14bea9'
GROUP_DUPES = set('''d8527cf3cf53f807937cce8bc3076aa175f50bb629673a3f8397f27c4a7a0110
33e72ec639742ffff4b68a915a5e61f7f7fe34ea5c29df62282c265abb356aef
5c145096f4e8ef4aa15ac3dfd12e4abb5ba6c36d494d303b2cec03cca23b3457
e07818f33c42f42fc3301a17f7c55c1df2875de0391307f8fbc852592104a347
3443a5eb0b1fef3337832318e5e105d6aea3e28ca763fcb6f495e592b623f328
b218ae818f8bf08f858d85d8b639236f4bbf2639b94a2eef42457f684d2076d9
6c0e923480c389def3fcf3c2c6c0e52e77e37c3f8ece8a2847c94288971050c6
b01f6d788f789f63bf8f86a30311646bed5e6f7a495d8677d604948a449ccb30'''.split())
LIVE_KEEP = 'c544f81fb098ba651e94f7e600e58a773d065a3baaa65caa3bf69de3717460a7'
LIVE_DUP = '04c4791ac98da70de68b452e20b92dda60f38d678bfc8af5aa8a21fe16905b82'


def validate_target(row, keep=None):
    if not row['source'].startswith('chat_summary:') or compute_paragraph_hash(row['content']) != row['hash']:
        raise ValueError('目标不是已核对的原生摘要正文')
    if keep and (keep['source'] != row['source'] or keep['hash'] == row['hash']):
        raise ValueError('不能跨会话合并或把保留项自身失效')


async def main():
    root = Path.cwd()
    backup = root / '.runtime/all-memory-review-20261001'
    if not (backup/'a-memorix/metadata/metadata.db').exists():
        raise RuntimeError('缺少全库备份，停止')
    store = AuditStore()
    kernel = SDKMemoryKernel(plugin_root=root,config=a_memorix_host_service._read_config())
    with patch.object(kernel,'_start_background_tasks',AsyncMock()):
        await kernel.initialize()
        try:
            all_rows = [dict(row) for row in kernel.metadata_store.query('SELECT * FROM paragraphs WHERE is_deleted=0')]
            for row in all_rows:
                row['metadata'] = metadata(row)
                row['type'] = 'paragraph'
            all_rows = kernel._get_search_hit_service()._filter_user_visible_hits(all_rows)
            effective = {row['hash']:row for row in all_rows if not (metadata(row).get('memory_change') or {}).get('valid_to')}
            targets = EMPTY | GROUP_DUPES | {LIVE_DUP}
            if not targets.issubset(effective):
                raise RuntimeError('核对范围已改变或已整理过，停止，不能重复执行')
            plan_entries = []
            for key in sorted(targets):
                row = effective[key]
                keep_key = GROUP_KEEP if key in GROUP_DUPES else LIVE_KEEP if key==LIVE_DUP else None
                keep = effective[keep_key] if keep_key else None
                validate_target(row,keep)
                plan_entries.append({'row':row,'keep':keep,'reason':'同一会话同义重复，无独有长期信息' if keep else '仅说明没有新事实，无具体长期事实；保留原始聊天与审计历史'})
            before_claims = kernel.metadata_store.query("SELECT * FROM fact_claims WHERE status IN ('active','conflicted')")
            with store.connect() as db:
                before_candidates = [dict(row) for row in db.execute('SELECT * FROM heart_candidates')]
            # 写入前先核对全部候选，避免先处理摘要、后发现候选归属不符。
            candidate_map = {row['id']:row for row in before_candidates}
            merges = {2:1,3:1,5:1,16:8,15:14}
            for old_id, keep_id in merges.items():
                old, keep = candidate_map[old_id], candidate_map[keep_id]
                if old['status'] != 'pending' or keep['status'] != 'pending' or (old['owner'],old['chat']) != (keep['owner'],keep['chat']):
                    raise RuntimeError('候选预检归属或状态改变，停止')
            for cid in (4,6,7):
                row = candidate_map[cid]
                if row['status'] != 'pending' or not visible(effective['0e330c4da63b82a28fc61c033fecb3fa8641c774a4bfb1c1bf0a3921ac4f797b'],row['chat'],row['owner']) or json.loads(row['payload'])['args']['text'] != '愁无寐喜欢自己':
                    raise RuntimeError('已有事实候选预检不符，停止')
            report = {'before':list(effective.values()),'plans':plan_entries,'facts':before_claims,'candidates_before':before_candidates}
            report_path = backup/'全量整理报告.json'
            report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
            sources = {row['source'] for row in effective.values()}
            for source in sources:
                if source.startswith('chat_summary:'):
                    historical = kernel.metadata_store.get_paragraphs_by_source(source)
                    cursor = max(int(metadata(row).get('trigger_message_count') or 0) for row in historical)
                    save_cursor(source.split(':',1)[1],cursor)
            for entry in plan_entries:
                row, keep, reason = entry['row'],entry['keep'],entry['reason']
                chat = row['source'].split(':',1)[1]
                operation = {'action':'mark_superseded','target_type':'paragraph','hash':row['hash'],'reason':reason,'valid_to':None}
                plan = {'scope':'memory','chat_id':chat,'operations':[operation],'reason':reason}
                record = kernel.metadata_store.create_fuzzy_modify_plan(request_text=reason,scope='memory',target_chat_id=chat,target_person_id='',plan=plan,
                    confidence=1.0,requested_by='heart.full_review',reason=reason,preview={'retained_hash':keep['hash'] if keep else None})
                result = await kernel.memory_correction_admin(action='execute',plan_id=record['plan_id'],confirmed=True,requested_by='heart.full_review',reason=reason)
                if not result.get('success'):
                    raise RuntimeError('原生修正失败，停止后续整理')
                store.append('摘要去重判断',chat,status='完整整理：重复摘要已合并' if keep else '完整整理：无事实摘要已失效',
                    old_memories=[row['content']],new_memory=keep['content'] if keep else '',reason=reason)
            # 候选仍保留全部原始证据，只标记重复，不将不确定内容擅自批准。
            with store.transaction() as db:
                for old_id,keep_id in merges.items():
                    old=db.execute('SELECT * FROM heart_candidates WHERE id=?',(old_id,)).fetchone()
                    keep=db.execute('SELECT * FROM heart_candidates WHERE id=?',(keep_id,)).fetchone()
                    if not old or not keep or old['status']!='pending' or keep['status']!='pending' or (old['owner'],old['chat'])!=(keep['owner'],keep['chat']):
                        raise RuntimeError('候选归属或状态改变，停止')
                    data=json.loads(old['payload']); data['organized_duplicate_of']=keep_id
                    db.execute("UPDATE heart_candidates SET status='skipped',payload=? WHERE id=?",(json.dumps(data,ensure_ascii=False),old_id))
                    store.append_in(db,'候选记忆',old['chat'],{'candidate_id':old_id,'status':'skipped','new_memory':data['args']['text'],
                        'reason':f'完整整理：与候选#{keep_id}同义，原始证据保留；保留项仍待审核，未写入长期记忆'})
                for cid in (4,6,7):
                    row=db.execute("SELECT * FROM heart_candidates WHERE id=? AND status='pending'",(cid,)).fetchone()
                    if not row:
                        raise RuntimeError('候选状态改变，停止')
                    data=json.loads(row['payload'])
                    target=effective['0e330c4da63b82a28fc61c033fecb3fa8641c774a4bfb1c1bf0a3921ac4f797b']
                    if not visible(target,row['chat'],row['owner']) or data['args']['text']!='愁无寐喜欢自己':
                        raise RuntimeError('候选不能核实为同一人同一会话已记事实')
                    db.execute("UPDATE heart_candidates SET status='skipped' WHERE id=?",(cid,))
                    store.append_in(db,'候选记忆',row['chat'],{'candidate_id':cid,'status':'skipped','new_memory':data['args']['text'],
                        'reason':'完整整理：同一会话本人已有“我喜欢自己”长期事实，未重复写入；原候选证据仍保留'})
            # 离线明确使用原生规则段落摘要，不调用模型，不更改正常运行的模型配置。
            segmenter=SimpleNamespace(generation_signature=lambda:{'mode':'offline_full_review'})
            episode_service=EpisodeService(metadata_store=kernel.metadata_store,segmentation_service=segmenter)
            async def offline_segment(**kwargs):
                return {'episodes':[episode_service._build_fallback_episode({'source':kwargs['source'],'paragraphs':kwargs['paragraphs']})],
                        'segmentation_model':'maintenance_rule','segmentation_version':'full-review-20261001'}
            segmenter.segment=offline_segment
            rebuilds=[]
            for source in sorted(sources):
                rebuilds.append(await episode_service.rebuild_source(source))
                chat = source.split(':',1)[1] if source.startswith('chat_summary:') else ''
                store.append('后台记忆管理',chat,status='情景索引离线重建完成',reviewer='本地完整整理',
                    reason='只读取有效正文，使用原生规则构建；未调用模型，未改写事实')
            after_claims=kernel.metadata_store.query("SELECT * FROM fact_claims WHERE status IN ('active','conflicted')")
            if before_claims!=after_claims:
                raise RuntimeError('人物事实账本意外改变，请停止并核对备份')
            remaining=[row for row in kernel.metadata_store.query('SELECT * FROM paragraphs WHERE is_deleted=0')
                       if not (metadata(row).get('memory_change') or {}).get('valid_to')]
            if targets & {row['hash'] for row in remaining}:
                raise RuntimeError('目标清理未完成')
            effective_keys = {row['hash'] for row in remaining}
            episodes = kernel.metadata_store.query('SELECT * FROM episodes')
            for episode in episodes:
                evidence = episode.get('evidence_ids') or []
                if isinstance(evidence,str):
                    evidence = json.loads(evidence)
                if not set(evidence).issubset(effective_keys):
                    raise RuntimeError('情景仍含失效证据，停止并核对')
            report.update({'after':remaining,'episode_rebuilds':rebuilds,'removed_count':len(plan_entries),'candidate_duplicates':8})
            report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
            with sqlite3.connect(f'file:{root / "data/MaiBot.db"}?mode=ro',uri=True) as host_db:
                names = {row[0]:row[1] or row[2] or '未命名会话' for row in host_db.execute('SELECT session_id,group_name,user_nickname FROM chat_sessions')}
            lines = ['# 全部会话记忆整理报告','', '已备份；不调用模型；不删除原始聊天。重复正文标记失效，原始审计证据仍保留。', '',
                     f'有效正文：{len(effective)} → {len(remaining)}；失效摘要：24；重复候选：8；人物事实：{len(before_claims)}条保持不变。', '']
            for chat,name in names.items():
                kept = [row for row in remaining if visible(dict(row),chat)]
                removed = [entry for entry in plan_entries if entry['row']['source']=='chat_summary:'+chat]
                if not kept and not removed:
                    continue
                lines.extend([f'## {name}', '', f'保留 {len(kept)} 条，整理 {len(removed)} 条。', '', '保留内容：', ''])
                lines.extend('- '+row['content'] for row in kept)
                lines.extend(['','整理内容及原因：',''])
                lines.extend('- '+entry['row']['content']+' —— '+entry['reason'] for entry in removed)
                lines.append('')
            lines.extend(['## 候选处理','', '候选2、3、5并入候选1；16并入8；15并入14。候选4、6、7与已有本人“喜欢自己”事实重复，标记跳过。',
                          '剩余候选仍待审核，没有擅自批准。各会话隔离，没有跨群或跨私聊合并。','',
                          '## 情景索引补丁','', '重建前过滤 memory_change.valid_to、mark_superseded 和 expires_at；离线使用有效正文生成规则摘要。',
                          '此次为 A_Memorix 本地临时兼容补丁，未提交上游。', ''])
            (backup/'整理结果说明.md').write_text('\n'.join(lines),encoding='utf-8')
            print(f'完整整理完成：{len(effective)}条有效正文→{len(remaining)}条；{len(plan_entries)}条摘要失效；8条候选标记重复；{len(before_claims)}条有效人物事实账本不变；{len(rebuilds)}个来源情景重建完成。')
        finally:
            await kernel.shutdown()


if __name__=='__main__':
    asyncio.run(main())
