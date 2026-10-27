# -*- coding: utf-8 -*-
"""SQLite 存储层 v3。

模型：
- jargon   = 已过审的词条（会注入）
- pending  = 待审/草稿（不注入）
- wiki     = 梗Wiki 快照（只读）
- 作用域 scope：'' = 全局，否则群号
- 来源 source：manual / ai

v2 -> v3 迁移：把 review_manual=1 的留在 jargon，其余搬进 pending，并删掉两个审核列。
"""

import json
import shutil
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jargon (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    meaning TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'ai',
    count INTEGER NOT NULL DEFAULT 1,
    auto_note TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(name, scope)
);
CREATE INDEX IF NOT EXISTS idx_jargon_scope ON jargon(scope);
CREATE INDEX IF NOT EXISTS idx_jargon_updated ON jargon(updated_at);
CREATE INDEX IF NOT EXISTS idx_jargon_name ON jargon(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    meaning TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'ai',
    count INTEGER NOT NULL DEFAULT 1,
    auto_note TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(name, scope)
);
CREATE INDEX IF NOT EXISTS idx_pending_scope ON pending(scope);
CREATE INDEX IF NOT EXISTS idx_pending_updated ON pending(updated_at);
CREATE INDEX IF NOT EXISTS idx_jargon_source ON jargon(source, updated_at);
CREATE TABLE IF NOT EXISTS wiki (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE,
    tags TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wiki_title ON wiki(title);
CREATE INDEX IF NOT EXISTS idx_wiki_updated ON wiki(updated_at);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_buffer_umo ON buffer(umo, ts);
"""

MAX_EVIDENCE = 3

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_jargon_scope ON jargon(scope);
CREATE INDEX IF NOT EXISTS idx_jargon_updated ON jargon(updated_at);
CREATE INDEX IF NOT EXISTS idx_jargon_name ON jargon(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_jargon_source ON jargon(source, updated_at);
CREATE INDEX IF NOT EXISTS idx_pending_scope ON pending(scope);
CREATE INDEX IF NOT EXISTS idx_pending_updated ON pending(updated_at);
CREATE INDEX IF NOT EXISTS idx_wiki_title ON wiki(title);
CREATE INDEX IF NOT EXISTS idx_wiki_updated ON wiki(updated_at);
CREATE INDEX IF NOT EXISTS idx_buffer_umo ON buffer(umo, ts);
"""


class JargonStore:
    def __init__(self, db_path: str):
        self._lock = threading.RLock()
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA wal_autocheckpoint=512")
        self._conn.execute("PRAGMA journal_size_limit=4194304")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_from_v1()
            self._migrate_v2_to_v3()
            self._migrate_v3_to_v4()
            self._conn.execute("DELETE FROM buffer WHERE ts < ?",
                               (time.time() - 7 * 86400,))
            self._conn.commit()

    # ---------------- 迁移 ----------------

    def _cols(self, table: str) -> set[str]:
        return {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def _migrate_from_v1(self):
        cols = self._cols("jargon")
        if "category" not in cols:
            return
        # v1 先按老规则并进 v2 结构，再由 v2->v3 收尾
        try:
            shutil.copy2(self.db_path, self.db_path + ".v1.bak")
        except Exception:
            pass
        rows = [dict(r) for r in self._conn.execute("SELECT * FROM jargon")]
        merged: dict[tuple[str, str], dict] = {}
        for r in rows:
            scope = "" if r.get("category") == "global" else (r.get("scope") or "")
            key = (r["name"], scope)
            cur = merged.get(key)
            if cur is None:
                r["_scope"] = scope
                r["_source"] = "manual" if r.get("category") == "manual" else "ai"
                merged[key] = r
                continue
            cur["count"] = cur.get("count", 1) + r.get("count", 1)
            if r.get("category") == "manual" and cur["_source"] != "manual":
                r["count"] = cur["count"]
                r["_scope"], r["_source"] = scope, "manual"
                merged[key] = r
        self._conn.execute("DROP TABLE IF EXISTS jargon")
        self._conn.executescript(SCHEMA)
        now = time.time()
        for (name, scope), r in merged.items():
            self._conn.execute(
                """INSERT INTO jargon(id, name, aliases, meaning, scope, source,
                   count, evidence, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (r.get("id"), name, r.get("aliases") or "", r.get("meaning") or "",
                 scope, r["_source"], r.get("count") or 1,
                 self._pack_evidence(r.get("evidence")),
                 r.get("created_at") or now, r.get("updated_at") or now))
        self._conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version','2')")
        self._conn.commit()

    def _migrate_v2_to_v3(self):
        cols = self._cols("jargon")
        if "review_manual" not in cols:
            return
        try:
            shutil.copy2(self.db_path, self.db_path + ".v2.bak")
        except Exception:
            pass
        approved = [dict(r) for r in self._conn.execute(
            "SELECT * FROM jargon WHERE review_manual = 1")]
        drafts = [dict(r) for r in self._conn.execute(
            "SELECT * FROM jargon WHERE review_manual = 0")]
        if "review_manual" in self._cols("pending"):
            drafts += [dict(r) for r in self._conn.execute("SELECT * FROM pending")]

        self._conn.execute("DROP TABLE IF EXISTS jargon")
        self._conn.execute("DROP TABLE IF EXISTS pending")
        self._conn.executescript(SCHEMA)
        for r in approved:
            self._insert_row("jargon", r)
        for r in drafts:
            self._insert_row("pending", r)
        self._conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version','3')")
        self._conn.commit()

    def _migrate_v3_to_v4(self):
        """v4：删掉两列从来没写过的字段，并补两个索引。"""
        jcols = self._cols("jargon")
        wcols = self._cols("wiki")
        if "origin_scope" not in jcols and "usage" not in wcols:
            return
        try:
            shutil.copy2(self.db_path, self.db_path + ".v3.bak")
        except Exception:
            pass
        jargon_rows = [dict(r) for r in self._conn.execute("SELECT * FROM jargon")]
        wiki_rows = [dict(r) for r in self._conn.execute("SELECT * FROM wiki")]
        self._conn.execute("ALTER TABLE jargon RENAME TO jargon_v3")
        self._conn.execute("ALTER TABLE wiki RENAME TO wiki_v3")
        self._conn.executescript(SCHEMA)
        for r in jargon_rows:
            self._conn.execute(
                """INSERT OR IGNORE INTO jargon(id, name, aliases, meaning, scope,
                   source, count, auto_note, evidence, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (r.get("id"), r.get("name"), r.get("aliases") or "",
                 r.get("meaning") or "", r.get("scope") or "",
                 r.get("source") or "ai", r.get("count") or 1,
                 r.get("auto_note") or "", self._pack_evidence(r.get("evidence")),
                 r.get("created_at") or time.time(),
                 r.get("updated_at") or time.time()))
        for r in wiki_rows:
            self._conn.execute(
                """INSERT OR IGNORE INTO wiki(id, title, tags, summary, updated_at)
                   VALUES(?,?,?,?,?)""",
                (r.get("id"), r.get("title"), r.get("tags") or "",
                 r.get("summary") or "", r.get("updated_at") or time.time()))
        self._conn.execute("DROP TABLE jargon_v3")
        self._conn.execute("DROP TABLE wiki_v3")
        self._conn.executescript(INDEXES)
        self._conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version','4')")
        self._conn.commit()

    def _insert_row(self, table: str, r: dict):
        self._conn.execute(
            f"""INSERT OR IGNORE INTO {table}(name, aliases, meaning, scope, source,
               count, auto_note, evidence, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (r.get("name"), r.get("aliases") or "", r.get("meaning") or "",
             r.get("scope") or "", r.get("source") or "ai", r.get("count") or 1,
             r.get("auto_note") or "", self._pack_evidence(r.get("evidence")),
             r.get("created_at") or time.time(), r.get("updated_at") or time.time()))

    @staticmethod
    def _pack_evidence(raw) -> str:
        if not raw:
            return "[]"
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return json.dumps(data[-MAX_EVIDENCE:], ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass
        return json.dumps([str(raw)[:300]], ensure_ascii=False)

    @staticmethod
    def _merge_evidence(old: str, new: str) -> str:
        try:
            items = json.loads(old or "[]")
            if not isinstance(items, list):
                items = []
        except (json.JSONDecodeError, TypeError):
            items = []
        if new and new not in items:
            items.append(new)
        return json.dumps(items[-MAX_EVIDENCE:], ensure_ascii=False)

    def close(self):
        with self._lock:
            try:
                self._conn.execute("PRAGMA optimize")
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass

    # ---------------- 缓冲 ----------------

    def buffer_add(self, umo: str, sender: str, text: str) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO buffer(umo, sender, text, ts) VALUES(?,?,?,?)",
                (umo, sender, text[:500], time.time()))
            self._conn.execute(
                "DELETE FROM buffer WHERE id <= (SELECT MAX(id) - 20000 FROM buffer)")
            self._conn.commit()
            return self._conn.execute(
                "SELECT COUNT(*) FROM buffer WHERE umo = ?", (umo,)).fetchone()[0]

    def buffer_take(self, umo: str, limit: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sender, text FROM buffer WHERE umo = ? ORDER BY id LIMIT ?",
                (umo, limit)).fetchall()
            return [dict(r) for r in rows]

    def buffer_drop(self, umo: str):
        with self._lock:
            self._conn.execute("DELETE FROM buffer WHERE umo = ?", (umo,))
            self._conn.commit()

    def buffer_clear(self, umo: str):
        self.buffer_drop(umo)

    # ---------------- 词条（已过审） ----------------

    def upsert(self, table: str, name: str, meaning: str, scope: str = "",
               aliases: str = "", evidence: str = "", source: str = "ai",
               auto_note: str = "") -> dict:
        name = (name or "").strip()[:32]
        meaning = (meaning or "").strip()[:1000]
        if not name or not meaning:
            return {}
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM {table} WHERE name = ? AND scope = ?",
                (name, scope)).fetchone()
            if row:
                rec = dict(row)
                self._conn.execute(
                    f"""UPDATE {table} SET meaning = ?, aliases = ?, count = count + 1,
                        evidence = ?, auto_note = ?, updated_at = ? WHERE id = ?""",
                    (meaning, aliases or rec["aliases"],
                     self._merge_evidence(rec["evidence"], evidence),
                     auto_note or rec["auto_note"], now, rec["id"]))
            else:
                self._conn.execute(
                    f"""INSERT INTO {table}(name, aliases, meaning, scope, source,
                        count, auto_note, evidence, created_at, updated_at)
                        VALUES(?,?,?,?,?,1,?,?,?,?)""",
                    (name, aliases, meaning, scope, source, auto_note,
                     self._pack_evidence(evidence), now, now))
            self._conn.commit()
        return self.get(name, table) or {}

    def add_manual(self, name: str, meaning: str, scope: str = "",
                   aliases: str = "") -> dict:
        return self.upsert("jargon", name, meaning, scope=scope,
                           aliases=aliases, evidence="手动录入", source="manual")

    def get(self, key: str, table: str | None = None) -> dict | None:
        tables = [table] if table else ["jargon", "pending"]
        with self._lock:
            for t in tables:
                if str(key).isdigit():
                    row = self._conn.execute(
                        f"SELECT * FROM {t} WHERE id = ?", (int(key),)).fetchone()
                    if row:
                        return dict(row)
                row = self._conn.execute(
                    f"SELECT * FROM {t} WHERE name = ? ORDER BY updated_at DESC LIMIT 1",
                    (str(key),)).fetchone()
                if row:
                    return dict(row)
        return None

    def eligible(self, scope: str) -> list[dict]:
        """注入候选：已过审的全部（jargon）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jargon WHERE scope = '' OR scope = ?", (scope,)).fetchall()
            return [dict(r) for r in rows]

    def query(self, bucket: str = "all", search: str = "",
              page: int = 1, size: int = 50) -> tuple[int, list[dict]]:
        where, params = [], []
        if bucket == "manual":
            where.append("source = 'manual'")
        elif bucket == "auto":
            where.append("source = 'ai'")
        elif bucket == "global":
            where.append("scope = ''")
        if search:
            where.append("(name LIKE ? OR meaning LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM jargon {sql_where}", params).fetchone()[0]
            rows = self._conn.execute(
                f"""SELECT * FROM jargon {sql_where}
                    ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                (*params, size, (page - 1) * size)).fetchall()
            return total, [dict(r) for r in rows]

    def query_page(self, bucket: str = "all", search: str = "", scope: str = "",
                   page: int = 1, size: int = 50) -> tuple[int, list[dict]]:
        where, params = [], []
        if bucket == "manual":
            where.append("source = 'manual'")
        elif bucket == "auto":
            where.append("source = 'ai'")
        if search:
            where.append("(name LIKE ? OR meaning LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        if scope:
            where.append("scope = ?")
            params.append(scope)
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM jargon {sql_where}", params).fetchone()[0]
            rows = self._conn.execute(
                f"""SELECT * FROM jargon {sql_where}
                    ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                (*params, size, (page - 1) * size)).fetchall()
            return total, [dict(r) for r in rows]

    def update(self, table: str, key: str, name: str | None = None,
               meaning: str | None = None, scope: str | None = None) -> dict | None:
        rec = self.get(key, table)
        if not rec:
            return None
        sets, params = [], []
        if name and name != rec["name"]:
            sets.append("name = ?")
            params.append(name[:32])
        if meaning:
            sets.append("meaning = ?")
            params.append(meaning[:1000])
        if scope is not None:
            sets.append("scope = ?")
            params.append(scope)
        if not sets:
            return rec
        params += [time.time(), rec["id"]]
        with self._lock:
            try:
                self._conn.execute(
                    f"UPDATE {table} SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                    params)
                self._conn.commit()
            except sqlite3.IntegrityError:
                return None
        return self.get(name or str(rec["id"]), table)

    def set_scope(self, key: str, scope: str) -> dict | None:
        return self.update("jargon", key, scope=scope)

    def rename(self, key: str, new_name: str) -> dict | None:
        return self.update("jargon", key, name=new_name)

    def delete(self, key: str) -> bool:
        rec = self.get(key)
        if not rec:
            return False
        with self._lock:
            table = "jargon" if self._conn.execute(
                "SELECT 1 FROM jargon WHERE id = ?", (rec["id"],)).fetchone() else "pending"
            self._conn.execute(f"DELETE FROM {table} WHERE id = ?", (rec["id"],))
            self._conn.commit()
        return True

    # ---------------- 审核（单标记） ----------------

    def submit(self, name: str, meaning: str, scope: str = "", aliases: str = "",
               evidence: str = "", auto_note: str = "", approved: bool = False) -> dict:
        """学习产物：模型自审通过 -> 直接进黑话库；否则进待审。"""
        table = "jargon" if approved else "pending"
        return self.upsert(table, name, meaning, scope=scope, aliases=aliases,
                           evidence=evidence, source="ai", auto_note=auto_note)

    def approve(self, key: str) -> str | None:
        """待审 -> 黑话库；已过审 -> 退回待审。返回动作名。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending WHERE name = ? ORDER BY updated_at DESC LIMIT 1",
                (key,)).fetchone()
            if row:
                rec = dict(row)
                self._upsert_approved(rec)
                self._conn.execute("DELETE FROM pending WHERE id = ?", (rec["id"],))
                self._conn.commit()
                return "approved"
            row = self._conn.execute(
                "SELECT * FROM jargon WHERE name = ? ORDER BY updated_at DESC LIMIT 1",
                (key,)).fetchone()
            if row:
                rec = dict(row)
                self._conn.execute(
                    """INSERT OR IGNORE INTO pending(name, aliases, meaning, scope,
                       source, count, auto_note, evidence, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (rec["name"], rec["aliases"], rec["meaning"], rec["scope"],
                     rec["source"], rec["count"], rec["auto_note"], rec["evidence"],
                     rec["created_at"], time.time()))
                self._conn.execute("DELETE FROM jargon WHERE id = ?", (rec["id"],))
                self._conn.commit()
                return "pending"
        return None

    def _upsert_approved(self, rec: dict):
        row = self._conn.execute(
            "SELECT * FROM jargon WHERE name = ? AND scope = ?",
            (rec["name"], rec["scope"])).fetchone()
        now = time.time()
        if row:
            cur = dict(row)
            self._conn.execute(
                """UPDATE jargon SET count = count + ?, meaning = ?,
                   evidence = ?, updated_at = ? WHERE id = ?""",
                (rec["count"], rec["meaning"],
                 self._merge_evidence(cur["evidence"], ""), now, cur["id"]))
        else:
            self._conn.execute(
                """INSERT INTO jargon(name, aliases, meaning, scope, source, count,
                   auto_note, evidence, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (rec["name"], rec["aliases"], rec["meaning"], rec["scope"],
                 rec["source"], rec["count"], rec["auto_note"], rec["evidence"],
                 rec["created_at"], now))

    # ---------------- 待审查询 ----------------

    def pending_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]

    def pending_scopes(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT scope, COUNT(*) n FROM pending GROUP BY scope").fetchall()
            return [{"scope": r[0] or "", "count": r[1]} for r in rows]

    def pending_query(self, search: str = "", page: int = 1,
                      size: int = 50) -> tuple[int, list[dict]]:
        where, params = "", []
        if search:
            where = "WHERE name LIKE ? OR meaning LIKE ?"
            params = [f"%{search}%", f"%{search}%"]
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM pending {where}", params).fetchone()[0]
            rows = self._conn.execute(
                f"""SELECT * FROM pending {where}
                    ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                (*params, size, (page - 1) * size)).fetchall()
            return total, [dict(r) for r in rows]

    # ---------------- 梗库 ----------------

    def wiki_upsert_many(self, rows: list[dict]) -> int:
        with self._lock:
            self._conn.executemany(
                """INSERT INTO wiki(title, tags, summary, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(title) DO UPDATE SET
                     tags = excluded.tags, summary = excluded.summary,
                     updated_at = excluded.updated_at""",
                [(r["title"], r.get("tags", ""), r.get("summary", ""),
                  r.get("updated_at") or time.time()) for r in rows])
            self._conn.commit()
            return self._conn.execute("SELECT COUNT(*) FROM wiki").fetchone()[0]

    def wiki_clear(self) -> int:
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM wiki").fetchone()[0]
            self._conn.execute("DELETE FROM wiki")
            self._conn.commit()
            return n

    def wiki_keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(r[0], r[1]) for r in
                    self._conn.execute("SELECT title, tags FROM wiki")]

    def wiki_get(self, title: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wiki WHERE title = ?", (title,)).fetchone()
            return dict(row) if row else None

    def wiki_query(self, search: str = "", page: int = 1,
                   size: int = 30) -> tuple[int, list[dict]]:
        where, params = "", []
        if search:
            where = "WHERE title LIKE ? OR summary LIKE ?"
            like = f"%{search}%"
            params = [like, like]
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM wiki {where}", params).fetchone()[0]
            rows = self._conn.execute(
                f"""SELECT * FROM wiki {where}
                    ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                (*params, size, (page - 1) * size)).fetchall()
            return total, [dict(r) for r in rows]

    def wiki_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM wiki").fetchone()[0]

    def meta_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
            return row[0] if row else None

    def meta_set(self, key: str, value: str):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (key, value))
            self._conn.commit()

    # ---------------- 统计 ----------------

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM jargon").fetchone()[0]
            manual = self._conn.execute(
                "SELECT COUNT(*) FROM jargon WHERE source = 'manual'").fetchone()[0]
            pending = self._conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
            global_n = self._conn.execute(
                "SELECT COUNT(*) FROM jargon WHERE scope = ''").fetchone()[0]
        return {"total": total, "manual": manual, "auto": total - manual,
                "pending": pending,
                "by_category": {"global": global_n, "group": total - global_n}}
