import unittest
from unittest.mock import patch
import templates
from application.ticket_flow import FlowResult
from domain.conversation import Conversation, ConvState
from tests._check_channel_route import flow, result, MAP_WANDA


class RecognitionNoticeTests(unittest.TestCase):
    def run_image(self, previous, actual, wanda_text):
        f, api, _ = flow(MAP_WANDA)
        api.ch7_recognize = lambda image, city: result()
        def quote(conv, recognition, out):
            out.channel = actual
            out.messages.append("报价结果")
            return out
        row = templates.find("RECOGNIZING_TIP")
        with patch("application.ticket_flow._night_cfg", return_value=None), patch.dict(row, {"text":"全局识别提示", "channel_text":{"wanda":wanda_text,"liangpiao":"良票识别提示"}}), patch.object(f, "_build_quote", side_effect=quote):
            return f.on_screenshot(Conversation(chat_id="c", channel=previous), "https://example.com/image")

    def test_empty_wanda_on_first_image(self):
        self.assertEqual(self.run_image(None,"wanda","").messages, ["报价结果"])

    def test_empty_wanda_after_liangpiao(self):
        self.assertEqual(self.run_image("liangpiao","wanda","").messages, ["报价结果"])

    def test_liangpiao_after_wanda_uses_liangpiao_copy(self):
        out = self.run_image("wanda","liangpiao","")
        self.assertEqual(out.messages[:2], ["良票识别提示","报价结果"])

    def test_wanda_custom_notice_once_and_first(self):
        out = self.run_image("liangpiao","wanda","万达自定义提示")
        self.assertEqual(out.messages[:2], ["万达自定义提示","报价结果"])
        self.assertEqual(out.messages.count("万达自定义提示"), 1)

    def test_manual_takeover_has_no_notice(self):
        f, _, _ = flow(MAP_WANDA)
        out = f.on_screenshot(Conversation(chat_id="c", state=ConvState.MANUAL), "https://example.com/image")
        self.assertEqual(out.messages, [])

if __name__ == "__main__":
    unittest.main()
