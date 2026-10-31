# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""存储层：四张词条表 + 通用 Repo。

词条按「来源 × 状态」物理拆表：

- manual / auto                      已过审（参与注入）
- manual_pending / auto_pending      待审（不注入）

每张表结构相同，唯一键 (name, scope)。id 只做内部主键，
对外操作一律用自然键 (表, 名称, 作用域)。
"""

import json
import shutil
import sqlite3
import threading
import time

APPROVED = ("manual", "auto")
PENDING = ("manual_pending", "auto_pending")
TABLES = APPROVED + PENDING

SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS {t} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '',
    meaning TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 1,
    auto_note TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    contributor TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(name, scope)
);
"""
SCHEMA_INDEX = """
CREATE INDEX IF NOT EXISTS idx_{t}_scope ON {t}(scope);
CREATE INDEX IF NOT EXISTS idx_{t}_updated ON {t}(updated_at);
CREATE INDEX IF NOT EXISTS idx_{t}_name ON {t}(name COLLATE NOCASE);
"""

MAX_EVIDENCE = 3


class Repo:
    """单表 CRUD。表名必须是白名单，避免拼接注入。"""

    def __init__(self, conn: sqlite3.Connection, table: str, lock):
        if table not in TABLES:
            raise ValueError(f"unknown table: {table}")
        self.t = table
        self.conn = conn
        self.lock = lock

    def find(self, name: str, scope: str = "") -> dict | None:
        with self.lock:
            row = self.conn.execute(
                f"SELECT * FROM {self.t} WHERE name = ? AND scope = ?",
                (name, scope)).fetchone()
            return dict(row) if row else None

    def get(self, rid: int) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                f"SELECT * FROM {self.t} WHERE id = ?", (rid,)).fetchone()
            return dict(row) if row else None

    def all(self, scope: str | None = None) -> list[dict]:
        with self.lock:
            if scope is None:
                rows = self.conn.execute(f"SELECT * FROM {self.t}").fetchall()
            else:
                rows = self.conn.execute(
                    f"SELECT * FROM {self.t} WHERE scope = '' OR scope = ?",
                    (scope,)).fetchall()
            return [dict(r) for r in rows]

    def insert(self, data: dict) -> dict:
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                f"""INSERT INTO {self.t}(name, aliases, keywords, meaning, scope,
                    count, auto_note, evidence, contributor, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (data["name"], data.get("aliases", ""), data.get("keywords", ""),
                 data.get("meaning", ""),
                 data.get("scope", ""), data.get("count", 1),
                 data.get("auto_note", ""), data.get("evidence", "[]"),
                 data.get("contributor", ""),
                 data.get("created_at") or now, data.get("updated_at") or now))
            self.conn.commit()
            rid = cur.lastrowid
        return self.get(rid)

    def update(self, rid: int, **fields) -> dict | None:
        allowed = ("name", "aliases", "keywords", "meaning", "scope",
                   "count", "auto_note", "evidence", "contributor")
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k} = ?")
                params.append(v)
        if not sets:
            return self.get(rid)
        sets.append("updated_at = ?")
        params.append(time.time())
        params.append(rid)
        with self.lock:
            try:
                self.conn.execute(
                    f"UPDATE {self.t} SET {', '.join(sets)} WHERE id = ?", params)
                self.conn.commit()
            except sqlite3.IntegrityError:
                return None
        return self.get(rid)

    def delete(self, rid: int) -> bool:
        with self.lock:
            cur = self.conn.execute(f"DELETE FROM {self.t} WHERE id = ?", (rid,))
            self.conn.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        with self.lock:
            return self.conn.execute(f"SELECT COUNT(*) FROM {self.t}").fetchone()[0]

    def scopes(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                f"SELECT scope, COUNT(*) n FROM {self.t} GROUP BY scope").fetchall()
            return [{"scope": r["scope"] or "", "count": r["n"]} for r in rows]

    def list_page(self, search: str = "", scope: str | None = None,
                  page: int = 1, size: int = 50, sort: str = "recent"):
        where, params = [], []
        if search:
            where.append("(name LIKE ? OR meaning LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        if scope is not None:
            where.append("scope = ?")
            params.append(scope)
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        order = "count DESC, updated_at DESC, id DESC" if sort == "count" \
            else "updated_at DESC, id DESC"
        with self.lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM {self.t} {sql_where}", params).fetchone()[0]
            rows = self.conn.execute(
                f"SELECT * FROM {self.t} {sql_where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, size, (page - 1) * size)).fetchall()
            return total, [dict(r) for r in rows]


class JargonStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=512")
        self.conn.execute("PRAGMA journal_size_limit=4194304")
        with self._lock:
            self._init_schema()
            self._migrate_legacy()
            if self.meta_get("schema_version") != "5":
                self.meta_set("schema_version", "5")
            self.conn.execute("DELETE FROM buffer WHERE ts < ?",
                              (time.time() - 7 * 86400,))
            self.conn.commit()
        self.repos = {t: Repo(self.conn, t, self._lock) for t in TABLES}

    def _init_schema(self):
        for t in TABLES:
            self.conn.execute(SCHEMA_TABLE.format(t=t))
            for stmt in SCHEMA_INDEX.format(t=t).split(";"):
                if stmt.strip():
                    self.conn.execute(stmt)
        self._ensure_columns()
        self.conn.executescript("""
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
        """)
        self.conn.commit()

    def _ensure_columns(self):
        """给四张表补 keywords 列，兼容 1.4.x 升级。"""
        for t in TABLES:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({t})")}
            if "keywords" not in cols:
                self.conn.execute(
                    f"ALTER TABLE {t} ADD COLUMN keywords TEXT NOT NULL DEFAULT ''")
            # 1.4.x 的 aliases 迁进 keywords，旧页面/旧数据仍能触发
            self.conn.execute(
                f"UPDATE {t} SET keywords = aliases "
                "WHERE keywords = '' AND aliases <> ''")
        self.conn.commit()

    # ---------- 旧库迁移 ----------

    def _migrate_legacy(self):
        """把 1.x（甚至更早）的 jargon/pending 拆进四张新表。

        兼容点：
        - v1：存在 category（global/group/manual），没有 source
        - v2：存在 review_manual / review_auto，未人工通过的应进待审
        - v3/v4：有 source，没有 contributor
        - 只有其中一张旧表也能迁
        迁移是幂等的：四张新表已有数据时，用 INSERT OR IGNORE 合并，不覆盖。
        """
        existing = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        old_tables = [t for t in ("jargon", "pending") if t in existing]
        if not old_tables:
            if self.meta_get("schema_version") is None:
                self.meta_set("schema_version", "5")
            return

        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{self.db_path}.pre-four-tables.{stamp}.bak"
        try:
            self.conn.execute("VACUUM INTO ?", (backup,))
        except Exception:
            try:
                shutil.copy2(self.db_path, backup)
            except Exception:
                pass

        try:
            for old in old_tables:
                cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({old})")}
                for r in self.conn.execute(f"SELECT * FROM {old}"):
                    row = dict(r)
                    name = (row.get("name") or "").strip()[:32]
                    meaning = (row.get("meaning") or "").strip()[:1000]
                    if not name or not meaning:
                        continue

                    source = str(row.get("source") or "").strip()
                    if source not in ("manual", "ai"):
                        source = "manual" if row.get("category") == "manual" else "ai"

                    approved = True
                    if old == "pending":
                        approved = False
                    elif "review_manual" in cols:
                        approved = bool(row.get("review_manual"))

                    if approved:
                        table = "manual" if source == "manual" else "auto"
                    else:
                        table = "manual_pending" if source == "manual" else "auto_pending"

                    scope = row.get("scope") or ""
                    if row.get("category") == "global":
                        scope = ""

                    self.conn.execute(
                        f"""INSERT OR IGNORE INTO {table}(name, aliases, keywords,
                            meaning, scope, count, auto_note, evidence, contributor,
                            created_at, updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (name, row.get("aliases") or "",
                         row.get("keywords") or row.get("aliases") or "",
                         meaning, scope,
                         row.get("count") or 1, row.get("auto_note") or "",
                         row.get("evidence") or "[]",
                         row.get("contributor") or "",
                         row.get("created_at") or time.time(),
                         row.get("updated_at") or time.time()))
            for old in old_tables:
                self.conn.execute(f"DROP TABLE {old}")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version', '5')")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- 访问器 ----------

    def repo(self, table: str) -> Repo:
        if table not in TABLES:
            raise ValueError(f"unknown table: {table!r}")
        return self.repos[table]

    @staticmethod
    def pack_evidence(raw) -> str:
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
    def merge_evidence(old: str, new: str) -> str:
        try:
            items = json.loads(old or "[]")
            if not isinstance(items, list):
                items = []
        except (json.JSONDecodeError, TypeError):
            items = []
        if new and new not in items:
            items.append(new)
        return json.dumps(items[-MAX_EVIDENCE:], ensure_ascii=False)

    # ---------- 缓冲（学习用） ----------

    def buffer_add(self, umo: str, sender: str, text: str) -> int:
        with self._lock:
            self.conn.execute(
                "INSERT INTO buffer(umo, sender, text, ts) VALUES(?,?,?,?)",
                (umo, sender, text[:500], time.time()))
            self.conn.execute(
                "DELETE FROM buffer WHERE id <= (SELECT MAX(id) - 20000 FROM buffer)")
            self.conn.commit()
            return self.conn.execute(
                "SELECT COUNT(*) FROM buffer WHERE umo = ?", (umo,)).fetchone()[0]

    def buffer_take(self, umo: str, limit: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, sender, text FROM buffer WHERE umo = ? ORDER BY id LIMIT ?",
                (umo, limit)).fetchall()
            return [dict(r) for r in rows]

    def buffer_delete(self, ids: list[int]):
        if not ids:
            return
        with self._lock:
            marks = ",".join("?" for _ in ids)
            self.conn.execute(f"DELETE FROM buffer WHERE id IN ({marks})", ids)
            self.conn.commit()

    def buffer_drop(self, umo: str):
        with self._lock:
            self.conn.execute("DELETE FROM buffer WHERE umo = ?", (umo,))
            self.conn.commit()

    # ---------- 梗库 ----------

    def wiki_upsert_many(self, rows: list[dict]) -> int:
        with self._lock:
            self.conn.executemany(
                """INSERT INTO wiki(title, tags, summary, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(title) DO UPDATE SET
                     tags = excluded.tags, summary = excluded.summary,
                     updated_at = excluded.updated_at""",
                [(r["title"], r.get("tags", ""), r.get("summary", ""),
                  r.get("updated_at") or time.time()) for r in rows])
            self.conn.commit()
            return self.conn.execute("SELECT COUNT(*) FROM wiki").fetchone()[0]

    def wiki_clear(self) -> int:
        with self._lock:
            n = self.conn.execute("SELECT COUNT(*) FROM wiki").fetchone()[0]
            self.conn.execute("DELETE FROM wiki")
            self.conn.commit()
            return n

    def wiki_keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(r[0], r[1]) for r in
                    self.conn.execute("SELECT title, tags FROM wiki")]

    def wiki_get(self, title: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM wiki WHERE title = ?", (title,)).fetchone()
            return dict(row) if row else None

    def wiki_query(self, search: str = "", page: int = 1,
                   size: int = 30) -> tuple[int, list[dict]]:
        where, params = "", []
        if search:
            where = "WHERE title LIKE ? OR summary LIKE ?"
            params = [f"%{search}%", f"%{search}%"]
        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM wiki {where}", params).fetchone()[0]
            rows = self.conn.execute(
                f"""SELECT * FROM wiki {where}
                    ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                (*params, size, (page - 1) * size)).fetchall()
            return total, [dict(r) for r in rows]

    def wiki_count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM wiki").fetchone()[0]

    def meta_get(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
            return row[0] if row else None

    def meta_set(self, key: str, value: str):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (key, value))
            self.conn.commit()

    def close(self):
        with self._lock:
            try:
                self.conn.execute("PRAGMA optimize")
            except Exception:
                pass
            try:
                self.conn.close()
            except Exception:
                pass
