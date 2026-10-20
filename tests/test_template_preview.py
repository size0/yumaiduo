"""Preview must show the selected channel and saves must stay in that channel."""

import copy
import json
import threading
import unittest
import tempfile
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server
import templates
from application import store


class TemplatePreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth = patch.object(server.Handler, "_auth_ok", return_value=True)
        cls.auth.start()
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.worker = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.worker.start()
        cls.url = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.worker.join()
        cls.auth.stop()

    def setUp(self):
        self.row = templates.find("QUOTE_CARD")
        self.original = copy.deepcopy(self.row)
        self.row["text"] = "基准 {cinema}[ggx1z82tg84]"
        self.row["channel_text"] = {"wanda": "万达 {cinema}",
                                    "liangpiao": "良票 {cinema}"}

    def tearDown(self):
        self.row.clear()
        self.row.update(self.original)

    def call(self, path, body=None):
        raw = json.dumps(body).encode() if body is not None else None
        request = Request(self.url + path, data=raw,
                          method="PUT" if body is not None else "GET",
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_preview_selects_channel_and_expands_only_its_references(self):
        wanda = self.call("/ta/api/preview/QUOTE_CARD?channel=wanda")
        liangpiao = self.call("/api/preview/QUOTE_CARD?channel=liangpiao")
        default = self.call("/api/preview/QUOTE_CARD")
        self.assertEqual(wanda["channel"], "wanda")
        self.assertTrue(wanda["text"].startswith("万达 "))
        self.assertTrue(liangpiao["text"].startswith("良票 "))
        self.assertNotIn("{cinema}", wanda["text"])
        self.assertEqual(len(wanda["parts"]), 1)
        self.assertGreater(len(default["parts"]), 1)

    def test_first_channel_save_leaves_base_and_other_channel_unchanged(self):
        self.row["channel_text"].pop("wanda")
        with patch.object(server, "save_templates") as save:
            self.call("/api/templates", {"key": "QUOTE_CARD",
                      "channel": "wanda", "text": "已修改 {cinema}"})
        save.assert_called_once()
        self.assertEqual(self.row["text"], "基准 {cinema}[ggx1z82tg84]")
        self.assertEqual(self.row["channel_text"]["liangpiao"], "良票 {cinema}")
        preview = self.call("/api/preview/QUOTE_CARD?channel=wanda")
        self.assertTrue(preview["text"].startswith("已修改 "))

    def test_unknown_channel_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.call("/api/preview/QUOTE_CARD?channel=unknown")
        self.assertEqual(error.exception.code, 400)

    def test_blank_channel_survives_save_and_reload_and_inherit_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(store, "TEMPLATES_FILE", Path(tmp) / "templates.json"):
                result = self.call("/api/templates", {"key": "QUOTE_CARD", "channel": "wanda", "text": ""})
                self.assertEqual(result["parts"], [])
                self.row["channel_text"] = {}
                store.load_template_overrides()
                self.assertIn("wanda", self.row["channel_text"])
                self.assertEqual(self.call("/api/preview/QUOTE_CARD?channel=wanda")["parts"], [])
                self.assertTrue(self.call("/api/preview/QUOTE_CARD?channel=liangpiao")["parts"])
                result = self.call("/api/templates", {"key": "QUOTE_CARD", "channel": "wanda", "text": "", "inherit": True})
                self.assertTrue(result["parts"])
                self.assertNotIn("wanda", self.row["channel_text"])

    def test_unknown_reference_is_not_sent_but_emoji_and_known_reference_remain(self):
        self.row["channel_text"]["wanda"] = "保留[送花][9psrbvfvc4c][ggx1z82tg84]"
        result = self.call("/api/preview/QUOTE_CARD?channel=wanda")
        self.assertNotIn("9psrbvfvc4c", result["text"])
        self.assertIn("[送花]", result["text"])
        self.assertEqual(len(result["parts"]), 2)

    def test_global_empty_suppresses_variant_and_render(self):
        self.row["text"] = ""
        self.row["variants"] = {"test": "不该回退到这里"}
        self.assertEqual(templates.render_messages("QUOTE_CARD", {"_variant": "test"}), [])
        self.assertEqual(templates.render("QUOTE_CARD", {"_variant": "test"}), "")

    def test_template_variant_can_be_saved_without_overwriting_base_copy(self):
        row = templates.find("WANDA_UNIT_PRICE")
        original = copy.deepcopy(row)
        try:
            with patch.object(server, "save_templates") as save:
                result = self.call("/api/templates", {
                    "key": "WANDA_UNIT_PRICE", "channel": "wanda",
                    "variant": "marked", "text": "需要几张票呢？",
                })
            save.assert_called_once()
            self.assertEqual(result["variants"]["marked"], "需要几张票呢？")
            self.assertIn("如需指定位置", row["text"])
            self.assertEqual(row["variants"]["marked"], "需要几张票呢？")
        finally:
            row.clear(); row.update(original)

    def test_restore_keeps_wanda_builtin_copy_after_reload(self):
        row = templates.find("TICKETING")
        original = copy.deepcopy(row)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(store, "TEMPLATES_FILE", Path(tmp) / "templates.json"):
                    self.call("/api/templates", {"key": "TICKETING", "channel": "wanda", "text": ""})
                    restored = self.call("/api/templates", {"key": "TICKETING", "channel": "wanda", "text": "", "inherit": True})
                    row["channel_text"] = {}
                    store.load_template_overrides()
                    self.assertEqual(templates.text_for("TICKETING", "wanda"), restored["effective"])
                    self.assertNotIn("{pickup_link}", restored["effective"])
        finally:
            row.clear()
            row.update(original)


if __name__ == "__main__":
    unittest.main()
