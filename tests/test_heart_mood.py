"""心情规则、持久化、并发隔离和真实 SDK Hook 测试；没有模型调用。"""

import asyncio
import importlib.util
import json
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions"))
from heart_shared.storage import AuditStore

spec = importlib.util.spec_from_file_location("test_mood_plugin", ROOT / "plugins/heart_mood/plugin.py",
                                             submodule_search_locations=[str(ROOT / "plugins/heart_mood")])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
from test_mood_plugin.engine import MoodEngine, appraise


class MoodTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.root = ROOT / ".runtime/test-data" / uuid.uuid4().hex
        self.store = AuditStore(self.root, clock=lambda: self.now)
        self.engine = MoodEngine(self.store)
        self.config = plugin.Config()

    def msg(self, text="谢谢你", mid="m1", sid="s1", user="synthetic-user"):
        return {"session_id": sid, "message_id": mid, "processed_plain_text": text,
                "message_info": {"user_info": {"user_id": user, "user_nickname": "测试"}}}

    def update(self, *args, **kwargs):
        return self.engine.update(self.msg(*args, **kwargs), self.config.mood, self.config.rules)

    def test_custom_intervals_and_style(self):
        self.update()
        states = plugin.States(entries=[
            plugin.EmotionState(name="安静", minimum=0, maximum=55, style="安静风格测试"),
            plugin.EmotionState(name="活跃", minimum=55, maximum=100, style="活跃风格测试")])
        prompt, _, band = self.engine.style("s1", self.config.prompts, states)
        self.assertEqual(band, "活跃")
        self.assertIn("活跃风格测试", prompt)
        self.assertIn(self.config.prompts.safety, prompt)
        with self.store.connect() as db:
            row = db.execute("SELECT detail FROM moods WHERE session='s1'").fetchone()
            detail = json.loads(row[0])
            detail["value"] = 100
            db.execute("UPDATE moods SET value=100,detail=? WHERE session='s1'", (json.dumps(detail),))
        self.assertEqual(self.engine.style("s1", self.config.prompts, states)[2], "活跃")

    def test_invalid_intervals_rejected(self):
        for start, end in ((1, 100), (0, 99), (50, 40)):
            with self.assertRaises(ValueError):
                plugin.States(entries=[plugin.EmotionState(minimum=start, maximum=end)])
        with self.assertRaises(ValueError):
            plugin.States(entries=[plugin.EmotionState(name="A", minimum=0, maximum=60),
                                   plugin.EmotionState(name="B", minimum=50, maximum=100)])

    def test_webui_object_list_schema(self):
        from maibot_sdk.config import generate_plugin_config_schema
        schema = generate_plugin_config_schema(plugin.Config)
        entries = schema["sections"]["states"]["fields"]["entries"]
        self.assertEqual(entries["item_type"], "object")
        self.assertEqual(set(entries["item_fields"]), {"name", "minimum", "maximum", "style"})

    def test_positive_negative_and_unknown(self):
        self.assertEqual(self.update()["value"], 55)
        self.assertEqual(self.update("你真没用", sid="s2")["value"], 44)
        self.assertEqual(self.update("我很难过", sid="s3")["value"], 48)
        self.assertEqual(self.update("Python怎么学", sid="s4")["value"], 50)

    def test_evidence_and_guarded_context(self):
        for text in ('假如他说你真没用', '“你是笨蛋”是什么意思', '/心情 谢谢你', '你不是笨蛋', '我不讨厌你'):
            self.assertEqual(appraise(text, self.config.rules)[0], 0, text)
        detail = self.update()
        self.assertEqual(detail["trigger"]["text"], "谢谢你")
        self.assertEqual(detail["evidence"][0]["evidence"], "谢谢你")

    def test_dedup_cooldown_and_isolation(self):
        self.assertEqual(self.update()["value"], 55)
        self.assertEqual(self.update()["value"], 55)
        self.assertEqual(self.update(mid="m2")["stimulus_delta"], 0)
        self.assertEqual(self.update(sid="another")["value"], 55)
        self.now += timedelta(seconds=61)
        self.assertEqual(self.update(mid="m3")["stimulus_delta"], 5)

    def test_recovery_restart_and_bounds(self):
        self.update()
        self.now += timedelta(hours=1)
        self.engine = MoodEngine(AuditStore(self.root, clock=lambda: self.now))
        self.assertEqual(self.update("普通消息", mid="m2")["value"], 53)
        self.config.mood.cooldown_seconds = 0
        for i in range(40):
            result = self.update(mid=f"pos-{i}")
        self.assertEqual(result["value"], 100)
        for i in range(40):
            result = self.update("你真没用", mid=f"neg-{i}")
        self.assertEqual(result["value"], 0)

    def test_concurrent_same_message_only_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: self.update()["value"], range(8)))
        self.assertEqual(values, [55] * 8)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_all_bands_and_invalid_configuration(self):
        self.update()
        for value, expected in [(20, "low"), (35, "neutral"), (65, "high"), (100, "high")]:
            with self.store.connect() as db:
                db.execute("UPDATE moods SET detail=? WHERE session='s1'", (json.dumps({"value": value}),))
            text, _, band = self.engine.style("s1", self.config.prompts)
            self.assertEqual(band, expected)
            self.assertIn("安全边界", text)
        with self.assertRaises(ValueError):
            plugin.Prompts(low_below=70, high_at_least=30)
        with self.assertRaises(ValueError):
            plugin.Settings(baseline=float("nan"))

    def test_hook_preserves_existing_prompt_and_shared_log(self):
        async def run():
            instance = plugin.create_plugin()
            instance.set_plugin_config({})
            instance.store, instance.engine = self.store, self.engine
            await instance.incoming(message=self.msg())
            kwargs = {"session_id": "s1", "reply_message_id": "m1", "extra_prompt": "队友原有要求", "model_name": "unchanged"}
            result = await instance.before_reply(**kwargs)
            self.assertEqual(kwargs["extra_prompt"], "队友原有要求")
            self.assertIn("队友原有要求", result["modified_kwargs"]["extra_prompt"])
            self.assertIn("55.0/100", result["modified_kwargs"]["extra_prompt"])
            self.assertEqual(result["modified_kwargs"]["model_name"], "unchanged")
            instance.set_plugin_config({"plugin": {"config_version": "1.0.0", "enabled": False}})
            self.assertNotIn("modified_kwargs", await instance.before_reply(**kwargs))
        asyncio.run(run())
        text = next((self.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("心情影响回复风格", text)
        self.assertIn("谢谢你", (self.root / "当前心情.txt").read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
