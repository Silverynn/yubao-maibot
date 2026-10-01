"""历史检索快照与当前库的显示分离；只用虚构记忆，不调用模型。"""

from pathlib import Path
import json
import sqlite3
import unittest
import uuid

from heart_shared.card_writer import CardWriter, flush_cards
from heart_shared.cards import detail_lines, render_cards
from heart_shared.recall_snapshot import annotate, read_catalog
from heart_shared.storage import AuditStore


def event(seq, kind='记忆操作结果', session='s1', **data):
    return {'seq':seq, 'kind':kind, 'session':session, 'day':'2026-10-01',
            'time':'2026-10-01T11:27:17+08:00', 'data':data}


def hit(key, text):
    return {'hash':key,'content':text,'metadata':{'chat_id':'s1','source_type':'chat_summary'}}


def native(key='live', text='已确认学习计划', **extra):
    return {'hash':key,'content':text,'source':'chat_summary:s1','metadata':{'chat_id':'s1'},
            'is_deleted':0, 'expires_at':None, **extra}


class RecallSnapshotTests(unittest.TestCase):
    def test_verified_cleaned_old_hits_fold_without_erasing_history(self):
        hits = [hit('old1','旧摘要一'),hit('old2','旧摘要二')]
        recall = event(1, operation='search_memory', outcome={'hits':hits})
        purge = event(2,'后台记忆管理',status='已删除核对后的重复或无事实正文',old_memories=['旧摘要一','旧摘要二'])
        annotate([recall,purge],[native()], 's1')
        text = '\n'.join(detail_lines(recall))
        self.assertIn('现已清理/失效：2 条历史候选',text)
        self.assertIn('历史检索快照',text)
        self.assertNotIn('历史候选 2：',text)
        self.assertEqual(recall['data']['outcome']['hits'],hits)

    def test_missing_catalog_never_asserts_deleted(self):
        recall=event(1,operation='search_memory',outcome={'hits':[hit('old','内容')]})
        purge=event(2,'后台记忆管理',status='已删除核对后的重复或无事实正文',old_memories=['内容'])
        annotate([recall,purge],None,'s1')
        self.assertFalse(recall['data']['_card_recall_view']['states'][0]['fold'])
        self.assertIn('未完成原生库只读核对','\n'.join(detail_lines(recall)))

    def test_cross_session_cleanup_not_used_as_proof(self):
        recall=event(1,operation='search_memory',outcome={'hits':[hit('old','内容')]})
        purge=event(2,'后台记忆管理',session='s2',status='已删除核对后的重复或无事实正文',old_memories=['内容'])
        annotate([recall,purge],[],'s1')
        state=recall['data']['_card_recall_view']['states'][0]
        self.assertFalse(state['fold'])
        self.assertIn('原因未确认',state['label'])

    def test_unknown_missing_hash_not_automatically_folded(self):
        recall=event(1,operation='search_memory',outcome={'hits':[hit('graph-id','关系记忆')]})
        annotate([recall],[],'s1')
        self.assertFalse(recall['data']['_card_recall_view']['states'][0]['fold'])
        self.assertIn('关系记忆','\n'.join(detail_lines(recall)))

    def test_negation_not_merged_by_similarity_or_prefix(self):
        recall=event(1,operation='search_memory',outcome={'hits':[hit('like','喜欢Python'),hit('dislike','不喜欢Python')]})
        annotate([recall],[native('like','喜欢Python'),native('dislike','不喜欢Python')],'s1')
        text='\n'.join(detail_lines(recall))
        self.assertIn('历史候选 1：',text)
        self.assertIn('历史候选 2：',text)
        self.assertNotIn('现已清理/失效',text)

    def test_exact_hit_list_repeated_in_same_card_not_expanded_twice(self):
        recall=event(1,operation='search_memory',message_id='m1',outcome={'hits':[hit('live','已确认学习计划')]})
        again=event(2,'群聊记忆检索',message_id='m1',hits=['已确认学习计划'])
        annotate([recall,again],[native()],'s1')
        info={'channel':'QQ','chat_type':'群聊','title':'虚构群','started':'2026-10-01 11:00:00'}
        messages={'m1':{'received':'2026-10-01T11:27:00+08:00','name':'虚构人物','text':'查看记忆'}}
        text=render_cards('2026-10-01',info,[recall,again],messages)
        self.assertEqual(text.count('历史候选 1：'),1)
        self.assertIn('此处不重复展开',text)
        self.assertIn('｜群聊记忆检索',text)

    def test_active_exact_text_not_hidden_by_older_cleanup_receipt(self):
        recall=event(1,'群聊记忆检索',hits=['已确认学习计划'])
        purge=event(2,'后台记忆管理',status='已删除核对后的重复或无事实正文',old_memories=['已确认学习计划'])
        annotate([recall,purge],[native()],'s1')
        self.assertFalse(recall['data']['_card_recall_view']['states'][0]['fold'])

    def test_native_deleted_and_superseded_flags_fold(self):
        recall=event(1,operation='search_memory',outcome={'hits':[hit('old','旧偏好')]})
        for row in [native('old','旧偏好',is_deleted=1),
                    native('old','旧偏好',metadata={'chat_id':'s1','memory_change':{'change_type':'mark_superseded'}})]:
            annotate([recall],[row],'s1')
            self.assertTrue(recall['data']['_card_recall_view']['states'][0]['fold'])

    def test_natural_group_string_hits_get_same_current_annotation(self):
        recall=event(1,'群聊记忆检索',hits=['旧摘要'])
        purge=event(2,'后台记忆管理',status='已删除核对后的重复或无事实正文',old_memories=['旧摘要'])
        annotate([recall,purge],[],'s1')
        self.assertIn('现已清理/失效：1 条','\n'.join(detail_lines(recall)))

    def test_read_catalog_missing_file_not_created(self):
        path=Path(__file__).resolve().parents[2]/'.runtime/test-data'/uuid.uuid4().hex/'absent.db'
        self.assertIsNone(read_catalog(path))
        self.assertFalse(path.exists())

    def test_live_export_annotates_read_only_and_refreshes_old_card_after_cleanup(self):
        root=Path(__file__).resolve().parents[2]/'.runtime/test-data'/uuid.uuid4().hex
        store=AuditStore(root/'data/heart_observation')
        catalog=root/'data/a-memorix/metadata/metadata.db'
        catalog.parent.mkdir(parents=True)
        with sqlite3.connect(catalog) as db:
            db.execute('CREATE TABLE paragraphs(hash TEXT,content TEXT,source TEXT,metadata TEXT,is_deleted INTEGER,expires_at REAL)')
            db.execute('INSERT INTO paragraphs VALUES(?,?,?,?,?,?)',('old','旧摘要','chat_summary:s1','{"chat_id":"s1"}',0,None))
        store.append('记忆操作结果','s1',operation='search_memory',outcome={'hits':[hit('old','旧摘要')]})
        flush_cards(store)
        with store.connect() as db:
            original=db.execute('SELECT payload FROM events ORDER BY seq LIMIT 1').fetchone()[0]
        with sqlite3.connect(catalog) as db: db.execute('DELETE FROM paragraphs')
        store.append('后台记忆管理','s1',status='已删除核对后的重复或无事实正文',old_memories=['旧摘要'])
        flush_cards(store)
        with store.connect() as db:
            self.assertEqual(original,db.execute('SELECT payload FROM events ORDER BY seq LIMIT 1').fetchone()[0])
            filename=db.execute('SELECT filename FROM card_exports').fetchone()[0]
        text=(store.root/'消息卡片'/filename).read_text(encoding='utf-8-sig')
        self.assertIn('现已清理/失效：1 条',text)
        self.assertEqual(read_catalog(catalog),[])


if __name__=='__main__':
    unittest.main()
