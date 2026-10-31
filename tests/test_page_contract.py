# -*- coding: utf-8 -*-
"""页面契约测试：动作请求必须携带 table/items，防止漏字段。"""

import pathlib
import unittest

PAGE = pathlib.Path(__file__).resolve().parents[1] / "pages" / "yh" / "index.html"


class TestPageContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_edit_sends_table(self):
        self.assertIn("table: editing.table", self.html)

    def test_edit_sends_old_name(self):
        self.assertIn("old_name: editing.content", self.html)

    def test_approve_sends_table(self):
        self.assertIn('"yh/approve", { table: it.table', self.html)

    def test_delete_sends_table(self):
        self.assertIn('"yh/delete", { table: it.table', self.html)

    def test_toggle_global_sends_table(self):
        self.assertIn('"yh/toggle_global", { table: it.table', self.html)

    def test_entry_fields(self):
        self.assertIn('id="editKeywords"', self.html)
        self.assertIn('id="addKeywords"', self.html)
        self.assertIn('id="editCount"', self.html)
        self.assertIn('id="addCount"', self.html)
        self.assertIn("keywords:", self.html)
        self.assertNotIn("editExtra", self.html)

    def test_add_preflight_lookup(self):
        self.assertIn('"yh/lookup"', self.html)
        self.assertIn("showConfirm", self.html)

    def test_meaning_limit_guard(self):
        self.assertIn("meaning.length > meaningLimit", self.html)
        self.assertIn("内容超长", self.html)

    def test_bulk_sends_items(self):
        self.assertIn('api("yh/bulk", { action, items })', self.html)

    def test_settings_groups_and_selects(self):
        self.assertIn("f.group_label", self.html)
        self.assertIn("Array.isArray(f.options)", self.html)
        self.assertIn("data-condition", self.html)


if __name__ == "__main__":
    unittest.main()
