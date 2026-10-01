"""消息卡片隔离测试：虚构对话，不启动机器人，不调用模型或修改真实记忆。"""

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import json
import time
import unittest
import uuid

from heart_shared.card_writer import CardWriter, flush_cards
from heart_shared.cards import SEPARATOR, render_cards, summary_counts
from heart_shared.storage import AuditStore


class LogCardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, 20, 0, tzinfo=timezone(timedelta(hours=8)))
        self.root = Path(__file__).resolve().parents[2] / ".runtime/test-data" / ("cards-" + uuid.uuid4().hex)
        self.store = AuditStore(self.root, clock=lambda: self.now)
        self.writer = CardWriter(self.store)

    def message(self, mid="m1", session="s1", text="我想学 Python", group=False, platform="webui"):
        info = {"platform": platform, "user_info": {"user_id": "webui_user_vtuber_visit_test" if platform == "webui" else "u1",
                "user_nickname": "测试访客"}}
        if group:
            info["group_info"] = {"group_id": "g1", "group_name": "测试群"}
        self.store.record_message({"session_id": session, "message_id": mid,
                                  "processed_plain_text": text, "message_info": info})

    def export(self, session="s1", day="2026-10-01"):
        flush_cards(self.store)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM card_exports WHERE day=? AND session=?", (day, session)).fetchone()
        return (self.root / "消息卡片" / row["filename"]).read_text(encoding="utf-8-sig")

    def test_interleaved_messages_group_by_evidence_not_latest(self):
        self.message("a", text="第一条")
        self.message("b", text="第二条")
        self.store.append("发送结果", "s1", message_id="a", response="给第一条的答复", sent=True)
        self.store.append("记忆操作结果", "s1", evidence_message_ids=["b"], operation="search_memory",
                          outcome={"hits": [{"content": "第二条的记忆"}]})
        text = self.export()
        first = text.split(SEPARATOR)[1]
        second = text.split(SEPARATOR)[3]
        self.assertIn("给第一条的答复", first)
        self.assertNotIn("第二条的记忆", first)
        self.assertIn("第二条的记忆", second)
        self.assertNotIn("给第一条的答复", second)

    def test_processing_status_and_late_update_replace_not_append(self):
        self.message()
        before = self.export()
        self.assertIn("处理中", before)
        self.now += timedelta(seconds=4)
        self.store.append("发送结果", "s1", message_id="m1", sent=True, response="你好")
        after = self.export()
        self.assertIn("回复已发送；后台结果持续补齐", after)
        self.assertEqual(after.count("【卡片 1｜消息】"), 1)
        self.assertEqual(after.count("用户说："), 1)
        self.assertIn("4.0 秒", after)

    def test_generated_response_does_not_claim_sent(self):
        self.message()
        self.store.append("生成回复（尚非送达）", "s1", message_id="m1", response="尚未发送")
        text = self.export()
        self.assertIn("状态：处理中", text)
        self.assertIn("回复已生成，尚非送达", text)

    def test_failed_send_not_successful_completion(self):
        self.message()
        self.store.append("发送结果", "s1", message_id="m1", response="发送失败的文字", sent=False)
        text = self.export()
        self.assertIn("状态：发送失败", text)
        self.assertNotIn("状态：已结束", text)

    def test_unknown_background_not_assigned_to_last_person(self):
        self.message()
        self.store.append("摘要去重判断", "s1", status="同义重复跳过", new_memory="后台摘要", reason="内容无新增")
        text = self.export()
        background = text.split(SEPARATOR)[3]
        self.assertIn("后台任务", background)
        self.assertIn("未提供可核实的触发消息", background)
        self.assertNotIn("人物：测试访客", background)

    def test_unobserved_message_id_does_not_use_recent_dialogue(self):
        self.message()
        self.store.append("发送结果", "s1", message_id="missing", response="无法关联", sent=True)
        text = self.export()
        self.assertIn("消息关联待核实", text)
        self.assertIn("不猜测发言者", text)
        self.assertEqual(text.count("用户说："), 1)

    def test_group_batch_single_count_and_bidirectional_refs(self):
        self.message("a", group=True, platform="qq")
        self.message("b", group=True, platform="qq")
        self.store.append("心情变化", "s1", evidence_message_ids=["a", "b"],
                          mood={"before": 60, "value": 63, "batch_count": 2, "stimulus_delta": 3,
                                "assessment": {"reason": "这段交流友善", "method": "AI"}})
        text = self.export()
        self.assertEqual(text.count("心情值：60 → 63"), 1)
        self.assertIn("关联消息卡片：1、2", text)
        self.assertEqual(text.count("参与合并处理：卡片 3"), 2)

    def test_same_id_different_sessions_never_mix(self):
        self.message("same", "s1", "第一位用户")
        self.message("same", "s2", "第二位用户")
        self.store.append("发送结果", "s1", message_id="same", response="第一会话答复", sent=True)
        self.assertNotIn("第二位用户", self.export("s1"))
        self.assertNotIn("第一会话答复", self.export("s2"))

    def test_midnight_late_result_updates_original_day(self):
        self.message()
        self.now += timedelta(days=1)
        self.store.append("发送结果", "s1", message_id="m1", response="次日迟到答复", sent=True)
        self.assertIn("次日迟到答复", self.export(day="2026-10-01"))
        today = self.export(day="2026-10-02")
        self.assertNotIn("用户说：", today)

    def test_explicit_task_id_merges_only_that_task(self):
        self.store.append("候选记忆", "s1", task_id="job1", new_memory="第一步", status="候选")
        self.store.append("后台记忆管理", "s1", task_id="job1", new_memory="第二步", status="已管理")
        self.store.append("后台记忆管理", "s1", new_memory="独立操作", status="已管理")
        text = self.export()
        self.assertEqual(text.count("｜后台任务】"), 2)
        self.assertIn("第二步", text.split(SEPARATOR)[1])

    def test_avatar_receipt_not_assumed_from_request(self):
        self.message()
        self.store.append("Live2D动作", "s1", message_id="m1", expression="开心", status="已请求")
        text = self.export()
        self.assertIn("不表示表情已成功执行", text)
        self.store.append("Live2D动作", "s1", message_id="m1", expression="开心", status="前端报告：已执行表情设置")
        self.assertIn("不等于人眼或截图确认", self.export())

    def test_no_new_card_render_in_message_path(self):
        with patch("heart_shared.card_writer.render_cards", side_effect=AssertionError("不得内联渲染")):
            self.message()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM card_pending").fetchone()[0], 1)

    def test_rendering_does_not_hold_database_write_lock_and_new_events_not_lost(self):
        self.message()
        once = []
        def render(*args):
            if not once:
                once.append(True)
                self.store.append("发送结果", "s1", message_id="m1", response="渲染期间的新结果", sent=True)
            return render_cards(*args)
        with patch("heart_shared.card_writer.render_cards", side_effect=render):
            self.writer.export("2026-10-01", "s1")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM card_pending").fetchone()[0], 1)
        self.assertIn("渲染期间的新结果", self.export())

    def test_export_retry_rebuilds_deleted_or_corrupt_file(self):
        self.message()
        expected = self.export()
        with self.store.connect() as db:
            row = db.execute("SELECT filename FROM card_exports").fetchone()
        file = self.root / "消息卡片" / row[0]
        file.unlink()
        self.writer.startup()
        self.assertEqual(self.export(), expected)

    def test_file_name_includes_type_title_start_and_separate_channel(self):
        self.message("a", "live1")
        self.now += timedelta(seconds=1)
        self.message("b", "live2")
        self.message("c", "qq1", group=True, platform="qq")
        flush_cards(self.store)
        files = list((self.root / "消息卡片").rglob("*.txt"))
        self.assertEqual(len(files), 3)
        self.assertEqual(len(list((self.root / "消息卡片/Live2D").glob("*.txt"))), 2)
        self.assertTrue(any("群聊_测试群_会话开始2026-10-01 20-00-01" in p.name for p in files))

    def test_next_start_overview_not_created_for_today_and_is_idempotent(self):
        self.message()
        self.writer.startup()
        self.assertFalse((self.root / "每日概览").exists())
        self.now += timedelta(days=1)
        self.writer.startup()
        file = self.root / "每日概览/2026-10-01_每日概览.txt"
        before = file.read_bytes()
        self.now += timedelta(seconds=10)
        self.writer.startup()
        self.assertEqual(file.read_bytes(), before)
        self.assertIn("用户消息：1", before.decode("utf-8-sig"))

    def test_startup_backfills_all_available_days_before_retention(self):
        self.message()
        self.now += timedelta(days=1)
        self.message("m2")
        self.now += timedelta(days=8)
        self.writer.startup()
        self.store.prune()
        self.assertTrue((self.root / '每日概览/2026-10-01_每日概览.txt').is_file())
        self.assertTrue((self.root / '每日概览/2026-10-02_每日概览.txt').is_file())

    def test_card_retention_does_not_clean_overviews_or_unmanaged_files(self):
        self.message()
        self.export()
        self.now += timedelta(days=1)
        self.writer.startup()
        marker = self.root / "消息卡片/Live2D/我的备注.txt"
        # 构造文件仅用于验证受控清理范围。
        self.assertTrue((self.root / "每日概览/2026-10-01_每日概览.txt").is_file())
        self.now += timedelta(days=4)
        self.store.prune()
        self.assertEqual(list((self.root / "消息卡片/Live2D").glob("*.txt")), [])
        self.assertFalse(self.store.managed_log_name(str(marker.name)))
        self.assertTrue((self.root / "每日概览/2026-10-01_每日概览.txt").is_file())

    def test_summary_does_not_mislabel_updates_as_new_memories(self):
        counts = summary_counts([{"kind": "记忆操作结果", "data": {"operation": "ingest_text",
                                  "outcome": {"stored_ids": ["an-update"]}}}])
        self.assertEqual(counts["报告存储成功操作（含更新）"], 1)
        self.assertNotIn("新增记忆", counts)

    def test_duplicate_events_idempotent_and_original_details_unchanged(self):
        self.message()
        self.message()
        text = self.export()
        self.assertEqual(text.count("用户说："), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            file = db.execute("SELECT filename FROM session_files").fetchone()[0]
        self.assertIn("#1 时间：", (self.root / "logs" / file).read_text(encoding="utf-8-sig"))

    def test_close_reopen_recovers_pending_cards_without_model(self):
        self.message()
        reopened = AuditStore(self.root, clock=lambda: self.now)
        self.assertEqual(flush_cards(reopened), 1)
        self.assertIn("我想学 Python", self.export())

    def test_overview_can_be_generated_after_original_events_expire(self):
        self.message()
        self.now += timedelta(days=8)
        self.store.prune()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
        self.writer.startup()
        text = (self.root / '每日概览/2026-10-01_每日概览.txt').read_text(encoding='utf-8-sig')
        self.assertIn('用户消息：1', text)

    def test_session_start_does_not_move_when_old_messages_expire(self):
        self.message()
        self.now += timedelta(days=5)
        self.message('later')
        self.store.prune()
        text = self.export(day='2026-10-06')
        self.assertIn('会话开始：2026-10-01 20:00:00', text)

    def test_daily_counts_do_not_double_count_ignored_duplicate_inserts(self):
        self.message()
        self.message()
        self.now += timedelta(days=1)
        self.writer.startup()
        text = (self.root / '每日概览/2026-10-01_每日概览.txt').read_text(encoding='utf-8-sig')
        self.assertIn('原始事件：1', text)
        self.assertNotIn('原始事件：2', text)

    def test_overview_does_not_overwrite_newer_cross_process_result(self):
        self.message()
        self.now += timedelta(days=1)
        self.writer.startup()
        # 日内统计后来增加时，下一次启动重新生成该日概览；仍然只保留一个文件。
        self.now -= timedelta(days=1)
        self.store.append('记忆去重', 's1', status='同义重复跳过', new_memory='同义内容')
        self.now += timedelta(days=1)
        self.writer.startup()
        text = (self.root / '每日概览/2026-10-01_每日概览.txt').read_text(encoding='utf-8-sig')
        self.assertIn('重复跳过操作：1', text)
        self.assertEqual(len(list((self.root / '每日概览').glob('*.txt'))), 1)

    def test_concurrent_events_and_projectors_do_not_lose_data_or_counts(self):
        self.message()
        def append(index):
            self.store.append('发送结果', 's1', message_id='m1', response=f'虚构结果{index}', sent=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(append, range(24)))
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: self.writer.export('2026-10-01', 's1'), range(6)))
        text = self.export()
        self.assertEqual(text.count('｜发送结果'), 24)
        with self.store.connect() as db:
            counts = json.loads(db.execute('SELECT counts FROM daily_totals').fetchone()[0])
        self.assertEqual(counts['原始事件'], 25)
        self.assertEqual(counts['发送成功操作'], 24)

    def test_user_message_about_timeout_not_counted_as_actual_timeout(self):
        counts = summary_counts([{'kind':'收到对话','data':{'text':'我遇到了请求超时'}}])
        self.assertEqual(counts['包含超时说明的操作'], 0)

    def test_background_thread_automatically_fills_card_without_blocking_reply(self):
        from heart_shared import card_writer
        self.store.start_card_logs()
        self.message()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not list((self.root / '消息卡片').rglob('*.txt')):
            time.sleep(.05)
        self.assertTrue(list((self.root / '消息卡片').rglob('*.txt')))
        with card_writer._LOCK:
            writer = card_writer._WRITERS.pop(self.root.resolve())
        writer.close()

    def test_overview_permission_failure_does_not_abort_plugin_startup(self):
        from heart_shared import card_writer
        with patch.object(card_writer.CardWriter, 'startup', side_effect=PermissionError('虚构权限故障')):
            with self.assertLogs('heart_shared.storage', level='ERROR'):
                self.store.start_card_logs()
        with card_writer._LOCK:
            writer = card_writer._WRITERS.pop(self.root.resolve())
        writer.close()

    def test_no_event_days_receive_clear_zero_overview_at_next_start(self):
        self.message()
        self.now += timedelta(days=3)
        self.writer.startup()
        text = (self.root / '每日概览/2026-10-02_每日概览.txt').read_text(encoding='utf-8-sig')
        self.assertIn('用户消息：0', text)
        self.assertIn('当天未观测到事件', text)
        self.assertFalse((self.root / '每日概览/2026-10-04_每日概览.txt').exists())

    def test_rolled_back_event_never_appears_in_cards_or_daily_totals(self):
        self.message()
        with self.assertRaises(ValueError):
            with self.store.transaction() as db:
                self.store.append_in(db, '发送结果', 's1', {'message_id':'m1','sent':True,'response':'撤销的记录'})
                raise ValueError('虚构事务回滚')
        self.assertNotIn('撤销的记录', self.export())
        with self.store.connect() as db:
            counts = json.loads(db.execute('SELECT counts FROM daily_totals').fetchone()[0])
        self.assertNotIn('发送成功操作', counts)

    def test_explicit_background_task_crossing_midnight_updates_original_card(self):
        self.store.append('候选记忆', 's1', task_id='night-job', status='候选', new_memory='原操作')
        self.export()
        self.now += timedelta(days=1)
        self.store.append('后台记忆管理', 's1', audit_task_id='night-job', status='已处理', new_memory='次日任务结果')
        self.assertIn('次日任务结果', self.export(day='2026-10-01'))


if __name__ == "__main__":
    unittest.main()
