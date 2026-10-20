"""Channel policy must hold for both agent lookup and no-model replies."""
import copy
import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from unittest.mock import patch

import qa
import server
from application import store
from application import conversation_ai, service
from integrations.model.client import ModelReply
from tests import test_buyer_assistant as fixtures


class FaqChannelTests(unittest.TestCase):
    def fixture(self, channel="wanda"):
        f = fixtures.BuyerAssistantTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        f.conv.channel = channel
        f.session.image_context = {}
        f.cfg.reply.intelligent_qa_enabled = True
        return f

    def assert_wanda_policy(self, text):
        self.assertIn("以店家实际出票进度为准", text)
        for forbidden in ("1-30", "50分钟", "已自动下单"):
            self.assertNotIn(forbidden, text)

    def test_lookup_uses_channel_policy_for_multiple_phrasings(self):
        for text in ("出票了吗", "怎么还没出票", "多久能出票", "出票时间"):
            with self.subTest(text=text):
                self.assert_wanda_policy(service._faq_lookup(text, channel="wanda"))

    def test_nested_reference_uses_same_channel(self):
        text = qa.resolve("常见问题：[utv1qjdlqg] [a6d4dqvkluc]", channel="wanda")
        self.assert_wanda_policy(text)

    def test_unknown_channel_does_not_inherit_liangpiao_commitment(self):
        text = service._faq_lookup("多久能出票", channel="")
        self.assertNotIn("1-30", text)
        self.assertNotIn("50分钟", text)
        self.assertNotIn("已自动下单", text)

    def test_liangpiao_policy_remains_distinct(self):
        text = service._faq_lookup("多久能出票", channel="liangpiao")
        self.assertIn("1-30", text)
        self.assertNotIn("以店家实际出票进度为准", text)

    def test_policy_does_not_mutate_shared_rule(self):
        before = qa.find_by_keyword("出票").content
        service._faq_lookup("多久能出票", channel="wanda")
        self.assertEqual(qa.find_by_keyword("出票").content, before)
        self.assertIn("1-30", service._faq_lookup("多久能出票", channel="liangpiao"))

    def test_unavailable_model_uses_wanda_policy_without_commands(self):
        for text in ("出票了吗", "怎么还没出票"):
            with self.subTest(text=text):
                f = self.fixture()
                f.svc.ai = SimpleNamespace(enabled=False, client=SimpleNamespace(ready=False))
                result = f.svc.on_message("c", text, config=f.cfg)
                self.assert_wanda_policy("\n".join(result.messages))
                self.assertEqual(result.pending_commands, [])
                self.assertEqual(f.orders.by_identity(f.ident), [])

    def test_legacy_saved_rule_keeps_channel_policy_on_reload(self):
        original = copy.deepcopy(qa.QA_RULES)
        self.addCleanup(lambda: (qa.QA_RULES.__setitem__(slice(None), original), qa.reindex()))
        rows = [{"qa_id": "utv1qjdlqg", "keyword": "出票", "content": "自定义良票时效20分钟"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "qa.json"
            path.write_text(json.dumps({"rules": rows}), encoding="utf-8")
            with patch.object(store, "QA_FILE", path):
                store.load_qa_overrides()
                self.assert_wanda_policy(service._faq_lookup("多久能出票", channel="wanda"))
                self.assertEqual(qa.reply("出票", channel="liangpiao"), "自定义良票时效20分钟")
                store.save_qa_rules(qa.QA_RULES)
                store.load_qa_overrides()
                self.assert_wanda_policy(service._faq_lookup("多久能出票", channel="wanda"))

    def test_edit_api_preserves_policy_without_new_field(self):
        original = copy.deepcopy(qa.QA_RULES)
        self.addCleanup(lambda: (qa.QA_RULES.__setitem__(slice(None), original), qa.reindex()))
        # Match the current UI payload, which only submits the original fields.
        rows = [{"qa_id": "utv1qjdlqg", "keyword": "出票", "content": "自定义良票时效20分钟"}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(store, "QA_FILE", Path(tmp) / "qa.json"), patch.object(server.Handler, "_auth_ok", return_value=True):
            http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            try:
                request = Request(f"http://127.0.0.1:{http.server_port}/api/qa",
                                  data=json.dumps({"rules": rows}).encode(), method="PUT",
                                  headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=3) as response:
                    self.assertTrue(json.load(response)["ok"])
                self.assert_wanda_policy(service._faq_lookup("多久能出票", channel="wanda"))
                self.assertEqual(qa.reply("出票", channel="liangpiao"), "自定义良票时效20分钟")
                store.load_qa_overrides()
                self.assert_wanda_policy(service._faq_lookup("多久能出票", channel="wanda"))
            finally:
                http.shutdown()
                http.server_close()
                thread.join()

    def test_invalid_channel_types_are_rejected(self):
        for channels in ([], "wanda", {"wanda": 123}, {"": "text"}):
            with self.subTest(channels=channels), self.assertRaises(TypeError):
                qa.QARule("aaaaaa", "示例", "正文", channel_contents=channels)

    def test_channel_reference_cycle_is_rejected_before_save(self):
        rows = [qa.QARule("aaaaaa", "甲", "普通正文", channel_contents={"wanda": "[bbbbbb]"}),
                qa.QARule("bbbbbb", "乙", "普通正文", channel_contents={"wanda": "[aaaaaa]"})]
        self.assertTrue(any("成环" in p for p in server._validate_qa(rows, qa)))

    def test_missing_channel_reference_is_rejected_before_save(self):
        rows = [qa.QARule("aaaaaa", "示例", "普通正文", channel_contents={"wanda": "[bbbbbb]"})]
        self.assertTrue(any("不存在" in p for p in server._validate_qa(rows, qa)))

    def test_model_error_does_not_fall_back_to_old_time_promise(self):
        f = self.fixture()

        class UnavailableModel:
            ready = True

            def chat(self, *_args, **_kwargs):
                raise TimeoutError("isolated model failure")

        f.svc.ai = conversation_ai.ConversationAI(UnavailableModel())
        result = f.svc.on_message("c", "怎么还没出票", config=f.cfg)
        self.assertTrue(result.need_human)
        self.assertEqual(result.pending_commands, [])
        self.assertNotIn("1-30", "\n".join(result.messages))
        self.assertNotIn("50分钟", "\n".join(result.messages))
        self.assertEqual(f.orders.by_identity(f.ident), [])

    def test_agent_gets_scoped_faq_result_and_no_transaction(self):
        for text in ("出票了吗", "怎么还没出票"):
            with self.subTest(text=text):
                f = self.fixture()
                model = fixtures.ScriptedModel([
                    fixtures.tool("record_buyer_intent", intent="other", evidence=text),
                    fixtures.tool("search_faq", keyword=text),
                    ModelReply(content="万达由店家处理出票，以店家实际出票进度为准。"),
                ])
                f.svc.ai = conversation_ai.ConversationAI(model)
                result = f.svc.on_message("c", text, config=f.cfg)
                self.assert_wanda_policy("\n".join(result.messages))
                answers = [json.loads(m["content"])["answer"]
                           for m in model.seen[-1][0]
                           if m.get("role") == "tool" and "answer" in json.loads(m["content"])]
                self.assertTrue(answers)
                for answer in answers:
                    self.assert_wanda_policy(answer)
                self.assertEqual(result.pending_commands, [])
                f.svc.flow.on_screenshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
