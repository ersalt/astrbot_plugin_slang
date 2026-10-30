# -*- coding: utf-8 -*-
"""服务层回归测试：CRUD、冲突、迁移、索引。

只依赖标准库，不加载 AstrBot：
    python -m unittest discover plugins/astrbot_plugin_slang/tests
"""

import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from index import KeywordIndex  # noqa: E402
from service import SlangError, SlangService  # noqa: E402
from store import JargonStore  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "jargon.db"
        self.store = JargonStore(str(self.db))
        self.svc = SlangService(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class TestManualCrud(Base):
    def test_add_update_rename_move_delete(self):
        self.svc.add_manual("大肥鱼", "胖鱼", scope="g1")
        row = self.store.repo("manual").find("大肥鱼", "g1")
        self.assertEqual(row["meaning"], "胖鱼")

        self.svc.update("manual", "大肥鱼", "g1", meaning="很胖的鱼")
        self.assertEqual(self.store.repo("manual").find("大肥鱼", "g1")["meaning"], "很胖的鱼")

        self.svc.update("manual", "大肥鱼", "g1", name="肥鱼")
        self.assertIsNone(self.store.repo("manual").find("大肥鱼", "g1"))
        self.assertIsNotNone(self.store.repo("manual").find("肥鱼", "g1"))

        self.svc.update("manual", "肥鱼", "g1", scope="")
        self.assertIsNotNone(self.store.repo("manual").find("肥鱼", ""))

        self.svc.delete("manual", "肥鱼", "")
        self.assertIsNone(self.store.repo("manual").find("肥鱼", ""))

    def test_duplicate_manual_rejected(self):
        self.svc.add_manual("重复", "第一版", scope="g1")
        with self.assertRaises(SlangError):
            self.svc.add_manual("重复", "第二版", scope="g1")
        self.assertEqual(self.store.repo("manual").find("重复", "g1")["meaning"], "第一版")

    def test_manual_and_auto_coexist(self):
        self.svc.submit("共存", "自动含义", scope="g1", approved=True)
        self.svc.add_manual("共存", "手动含义", scope="g1")
        self.assertEqual(self.store.repo("auto").find("共存", "g1")["meaning"], "自动含义")
        self.assertEqual(self.store.repo("manual").find("共存", "g1")["meaning"], "手动含义")


class TestLearning(Base):
    def test_submit_pending_and_approved(self):
        self.svc.submit("待审词", "草稿", scope="g1", approved=False)
        self.assertIsNotNone(self.store.repo("auto_pending").find("待审词", "g1"))
        self.svc.submit("过审词", "含义", scope="g1", approved=True)
        self.assertIsNotNone(self.store.repo("auto").find("过审词", "g1"))

    def test_duplicate_submit_only_counts(self):
        self.svc.submit("累加", "原含义", scope="g1", approved=True)
        self.svc.submit("累加", "新含义", scope="g1", approved=True)
        row = self.store.repo("auto").find("累加", "g1")
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["meaning"], "原含义")


class TestConflicts(Base):
    def test_rename_conflict(self):
        self.svc.add_manual("A", "a", scope="g1")
        self.svc.add_manual("B", "b", scope="g1")
        with self.assertRaises(SlangError):
            self.svc.update("manual", "B", "g1", name="A")

    def test_move_scope_conflict(self):
        self.svc.add_manual("A", "a", scope="g1")
        self.svc.add_manual("A", "a2", scope="g2")
        with self.assertRaises(SlangError):
            self.svc.update("manual", "A", "g2", scope="g1")

    def test_approve_conflict_keeps_both(self):
        self.svc.submit("冲突", "自动已审", scope="g1", approved=True)
        self.svc.submit("冲突", "自动待审", scope="g1", approved=False)
        with self.assertRaises(SlangError):
            self.svc.approve("auto_pending", "冲突", "g1")
        self.assertEqual(self.store.repo("auto").find("冲突", "g1")["meaning"], "自动已审")
        self.assertEqual(
            self.store.repo("auto_pending").find("冲突", "g1")["meaning"], "自动待审")

    def test_approve_moves_row(self):
        self.svc.submit("通过", "含义", scope="g1", approved=False)
        self.assertEqual(self.svc.approve("auto_pending", "通过", "g1"), "approved")
        self.assertIsNone(self.store.repo("auto_pending").find("通过", "g1"))
        self.assertIsNotNone(self.store.repo("auto").find("通过", "g1"))

    def test_reject_conflict_keeps_both(self):
        self.svc.submit("退回", "已审", scope="g1", approved=True)
        self.svc.submit("退回", "待审", scope="g1", approved=False)
        with self.assertRaises(SlangError):
            self.svc.approve("auto", "退回", "g1")
        self.assertIsNotNone(self.store.repo("auto").find("退回", "g1"))
        self.assertIsNotNone(self.store.repo("auto_pending").find("退回", "g1"))

    def test_bulk_skips_conflict(self):
        self.svc.submit("批量", "已审", scope="g1", approved=True)
        self.svc.submit("批量", "待审", scope="g1", approved=False)
        self.svc.submit("正常", "待审", scope="g1", approved=False)
        res = self.svc.bulk("approve", [
            {"table": "auto_pending", "name": "批量", "scope": "g1"},
            {"table": "auto_pending", "name": "正常", "scope": "g1"},
        ])
        self.assertEqual(res["approved"], 1)
        self.assertEqual(res["skipped"], 1)


class TestIndex(Base):
    def test_manual_priority_and_scope(self):
        self.svc.submit("大肥鱼", "自动含义", scope="g1", approved=True)
        self.svc.add_manual("大肥鱼", "手动含义", scope="g1")
        hits = self.svc.match("今天大肥鱼来了", "g1", 3)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["source"], "manual")
        self.assertEqual(hits[0]["meaning"], "手动含义")

    def test_scope_filter(self):
        self.svc.submit("群内词", "群 g1", scope="g1", approved=True)
        self.assertEqual(self.svc.match("群内词", "g2", 3), [])
        self.assertEqual(len(self.svc.match("群内词", "g1", 3)), 1)

    def test_alias_match(self):
        self.svc.add_manual("梁文锋", "DeepSeek 创始人", scope="", aliases="梁文谷,梁文峰")
        hits = self.svc.match("梁文谷今天说话了", "", 3)
        self.assertEqual(hits[0]["name"], "梁文锋")


class TestMigration(Base):
    def _old_db(self, jargon_sql: str, pending_sql: str | None = None):
        path = pathlib.Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(jargon_sql)
        if pending_sql:
            conn.executescript(pending_sql)
        conn.commit()
        conn.close()
        return path

    def test_v3_split(self):
        path = self._old_db("""
        CREATE TABLE jargon (id INTEGER PRIMARY KEY, name TEXT, aliases TEXT, meaning TEXT,
            scope TEXT, source TEXT, count INTEGER, auto_note TEXT, evidence TEXT,
            contributor TEXT, created_at REAL, updated_at REAL, UNIQUE(name, scope));
        INSERT INTO jargon(name,aliases,meaning,scope,source,count,auto_note,evidence,contributor,created_at,updated_at)
        VALUES('手动词','','手动含义','g1','manual',3,'','[]','贰叔',1,1),
              ('自动词','','自动含义','','ai',2,'','[]','',1,1);
        CREATE TABLE pending (id INTEGER PRIMARY KEY, name TEXT, aliases TEXT, meaning TEXT,
            scope TEXT, source TEXT, count INTEGER, auto_note TEXT, evidence TEXT,
            contributor TEXT, created_at REAL, updated_at REAL, UNIQUE(name, scope));
        INSERT INTO pending(name,aliases,meaning,scope,source,count,auto_note,evidence,contributor,created_at,updated_at)
        VALUES('待审词','','草稿','g1','ai',1,'','[]','',1,1);
        """)
        store = JargonStore(str(path))
        try:
            self.assertEqual(store.repo("manual").find("手动词", "g1")["meaning"], "手动含义")
            self.assertEqual(store.repo("auto").find("自动词", "")["count"], 2)
            self.assertEqual(store.repo("auto_pending").find("待审词", "g1")["meaning"], "草稿")
            self.assertIsNone(store.repo("auto").find("待审词", "g1"))
        finally:
            store.close()

    def test_v2_review_flags(self):
        path = self._old_db("""
        CREATE TABLE jargon (id INTEGER PRIMARY KEY, name TEXT, meaning TEXT, scope TEXT,
            source TEXT, review_manual INTEGER DEFAULT 0, review_auto INTEGER DEFAULT 0,
            count INTEGER DEFAULT 1, evidence TEXT DEFAULT '[]', created_at REAL, updated_at REAL,
            UNIQUE(name, scope));
        INSERT INTO jargon(name,meaning,scope,source,review_manual,review_auto,count,created_at,updated_at)
        VALUES('已审','含义','g1','ai',1,1,2,1,1),
              ('未审','草稿','g1','ai',0,0,1,1,1);
        """)
        store = JargonStore(str(path))
        try:
            self.assertIsNotNone(store.repo("auto").find("已审", "g1"))
            self.assertIsNotNone(store.repo("auto_pending").find("未审", "g1"))
        finally:
            store.close()

    def test_v1_category(self):
        path = self._old_db("""
        CREATE TABLE jargon (id INTEGER PRIMARY KEY, name TEXT, meaning TEXT, scope TEXT,
            category TEXT, count INTEGER DEFAULT 1, created_at REAL, updated_at REAL,
            UNIQUE(name, scope));
        INSERT INTO jargon(name,meaning,scope,category,count,created_at,updated_at)
        VALUES('全局词','含义','','global',1,1,1),
              ('手动词','含义','','manual',1,1,1),
              ('群词','含义','g1','group',1,1,1);
        """)
        store = JargonStore(str(path))
        try:
            self.assertIsNotNone(store.repo("auto").find("全局词", ""))
            self.assertIsNotNone(store.repo("manual").find("手动词", ""))
            self.assertIsNotNone(store.repo("auto").find("群词", "g1"))
        finally:
            store.close()

    def test_reopen_is_idempotent(self):
        self.svc.add_manual("保留", "含义", scope="g1")
        self.store.close()
        store2 = JargonStore(str(self.db))
        try:
            self.assertIsNotNone(store2.repo("manual").find("保留", "g1"))
            self.assertEqual(store2.meta_get("schema_version"), "4")
        finally:
            store2.close()


if __name__ == "__main__":
    unittest.main()
