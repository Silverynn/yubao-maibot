"""AI判断使用虚构模型返回，不调用付费服务。"""
# ruff: noqa: I001 -- 先由测试加载器注册动态插件包，再导入其模块。
import asyncio
import json
import logging
import unittest
import uuid
from types import SimpleNamespace

from test_heart_mood import ROOT, MoodEngine, plugin
from heart_shared.storage import AuditStore
from test_mood_plugin.appraiser import AIAppraiser, parse_assessment


class AIMoodTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = AuditStore(ROOT / ".runtime/test-data" / uuid.uuid4().hex)
        self.config = plugin.Config(ai=plugin.AISettings(enabled=True))
        self.count = 0
        self.answer = {"delta": -3, "confidence": .9, "reason": "反讽而非真实赞扬", "evidence": "你真棒"}
        self.p = SimpleNamespace(store=self.store, engine=MoodEngine(self.store), config=self.config,
            ctx=SimpleNamespace(call_capability=self.call), _get_logger=lambda: logging.getLogger("test"))
        self.ai = AIAppraiser(self.p)

    async def asyncTearDown(self):
        await self.ai.close()

    async def call(self, capability, **kwargs):
        self.count += 1
        self.assertEqual(capability, "llm.generate")
        self.assertEqual(kwargs["task_name"], "utils")
        self.assertEqual(kwargs["max_tokens"], self.config.ai.max_output_tokens)
        return {"success": True, "response": json.dumps(self.answer, ensure_ascii=False), "reasoning": "不应记录的隐藏推理"}

    def message(self, mid="one", sid="chat"):
        return {"session_id": sid, "message_id": mid, "processed_plain_text": "又把事情搞错了，你真棒。",
                "message_info": {"user_info": {"user_id": "user", "user_nickname": "虚构同学"}}}

    def state(self, sid="chat"):
        with self.store.connect() as db:
            return self.store.mood_snapshot(db, sid)

    async def finish(self, **kwargs):
        await self.ai.submit(self.message(**kwargs))
        await asyncio.wait_for(self.ai.queue.join(), 3)

    async def test_ai_overrides_keyword_and_logs(self):
        await self.finish()
        state = self.state()
        self.assertEqual(state["value"], 47)
        self.assertEqual(state["assessment"]["method"], "AI判断")
        text = next((self.store.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("反讽而非真实赞扬", text)
        self.assertIn("实际总变化：-3", text)
        self.assertNotIn("隐藏推理", text)

    async def test_duplicate_no_double_call_or_score(self):
        await asyncio.gather(self.ai.submit(self.message()), self.ai.submit(self.message()))
        await self.ai.queue.join()
        await self.finish()
        self.assertEqual(self.count, 1)
        self.assertEqual(self.state()["value"], 47)

    async def test_low_confidence_no_keyword_fallback(self):
        self.config.ai.fallback_keywords = True
        self.answer["confidence"] = .2
        await self.finish()
        self.assertEqual(self.state()["value"], 50)
        self.assertIn("把握不足", self.state()["reason"])

    async def test_failure_visible_without_keyword(self):
        self.answer["delta"] = "wrong"
        await self.finish()
        self.assertEqual(self.state()["value"], 50)
        self.assertEqual(self.state()["assessment"]["method"], "AI未完成")

    async def test_optional_failure_keyword_fallback(self):
        self.config.ai.fallback_keywords = True
        self.answer["delta"] = "wrong"
        await self.finish()
        self.assertEqual(self.state()["value"], 55)
        self.assertEqual(self.state()["assessment"]["method"], "AI失败后关键词")

    async def test_limits_cooldown_and_isolation(self):
        self.answer["delta"] = 20
        await self.finish()
        self.assertEqual(self.state()["value"], 58)
        await self.finish(mid="two")
        self.assertEqual(self.state()["stimulus_delta"], 0)
        self.assertEqual(self.state()["assessment"]["model_delta"], 20)
        await self.finish(mid="three", sid="other")
        self.assertEqual(self.state("other")["value"], 58)

    async def test_disable_while_waiting_does_not_apply(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def call(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"success": True, "response": json.dumps(self.answer)}
        self.p.ctx.call_capability = call
        await self.ai.submit(self.message())
        await entered.wait()
        self.config.ai.enabled = False
        release.set()
        await self.ai.queue.join()
        self.assertIsNone(self.state().get("value"))

    async def test_timeout_visible(self):
        async def call(*args, **kwargs):
            raise TimeoutError()
        self.p.ctx.call_capability = call
        await self.finish()
        self.assertIn("超时", self.state()["reason"])

    def test_malformed_and_invented_evidence_rejected(self):
        for patch in ({"delta": True}, {"delta": float("nan")}, {"delta": 21}, {"confidence": 2},
                      {"evidence": "并不存在的原话"}, {"reason": ""}):
            data = {**self.answer, **patch}
            with self.assertRaises(ValueError):
                parse_assessment(json.dumps(data), self.message()["processed_plain_text"], .7)

    def test_config_default_off_and_webui_fields(self):
        from maibot_sdk.config import generate_plugin_config_schema
        self.assertFalse(plugin.Config().ai.enabled)
        fields = generate_plugin_config_schema(plugin.Config)["sections"]["ai"]["fields"]
        self.assertIn("enabled", fields)
        self.assertIn("guidance", fields)
        for name in ("max_output_tokens", "delta_multiplier", "positive_limit", "negative_limit"):
            self.assertIn(name, fields)

    def test_complete_json_wrappers_only(self):
        raw = json.dumps(self.answer, ensure_ascii=False)
        for wrapped in (raw, "```json\n" + raw + "\n```", "评估结果：\n" + raw + "\n以上为评分。"):
            self.assertEqual(parse_assessment(wrapped, self.message()["processed_plain_text"], .7)["delta"], -3)
        for broken in (raw[:-1], raw + raw, '[ ' + raw + ' ]', '<think>解释</think>' + raw,
                       '{"delta":1,"delta":2,"confidence":1,"reason":"测试","evidence":"你真棒"}'):
            with self.assertRaises(ValueError):
                parse_assessment(broken, self.message()["processed_plain_text"], .7)

    async def test_budget_and_empty_response_diagnosis(self):
        async def call(*args, **kwargs):
            return {"success": True, "response": "", "completion_tokens": kwargs["max_tokens"], "reasoning": "禁止存储"}
        self.p.ctx.call_capability = call
        await self.finish()
        assessment = self.state()["assessment"]
        self.assertEqual(assessment["error_code"], "empty_response")
        self.assertTrue(assessment["diagnostics"]["budget_reached"])
        self.assertEqual(assessment["diagnostics"]["output_units_used"], 4096)
        self.assertIn("疑似", assessment["reason"])
        self.assertNotIn("禁止存储", json.dumps(assessment, ensure_ascii=False))

    async def test_multiplier_and_directional_limits(self):
        self.config.ai.delta_multiplier = 2
        self.config.ai.negative_limit = 4
        self.config.ai.max_output_tokens = 2048
        await self.finish()
        state = self.state()
        self.assertEqual(state["assessment"]["scaled_delta"], -6)
        self.assertEqual(state["stimulus_delta"], -4)
        self.assertEqual(state["value"], 46)
        self.answer["delta"] = 3
        self.config.ai.positive_limit = 5
        await self.finish(sid="positive")
        self.assertEqual(self.state("positive")["value"], 55)

    async def test_zero_multiplier_keeps_state(self):
        self.config.ai.delta_multiplier = 0
        await self.finish()
        self.assertEqual(self.state()["value"], 50)

    async def test_low_confidence_remains_zero_after_multiplier(self):
        self.config.ai.delta_multiplier = 3
        self.answer["confidence"] = .1
        await self.finish()
        self.assertEqual(self.state()["stimulus_delta"], 0)

    async def test_global_limit_still_applies(self):
        self.config.ai.delta_multiplier = 3
        self.config.ai.positive_limit = 20
        self.config.mood.max_delta = 2
        self.answer["delta"] = 10
        await self.finish()
        self.assertEqual(self.state()["value"], 52)

    def test_invalid_settings_rejected(self):
        for patch in ({"max_output_tokens": 0}, {"delta_multiplier": float("nan")}, {"positive_limit": -1},
                      {"negative_limit": 21}, {"delta_multiplier": 4}, {"timeout_seconds": 61}):
            with self.assertRaises(ValueError):
                plugin.AISettings(**patch)
