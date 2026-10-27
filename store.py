# -*- coding: utf-8 -*-
"""SQLite 存储层。只依赖标准库，方便脱离 AstrBot 单测。"""

import json
import sqlite3
import threading
import time

CATEGORIES = ("global", "group", "manual")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jargon (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    meaning TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'group',
    scope TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 1,
    auto_review INTEGER NOT NULL DEFAULT 0,
    manual_review INTEGER NOT NULL DEFAULT 0,
    auto_note TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(name, category, scope)
);
CREATE INDEX IF NOT EXISTS idx_jargon_scope ON jargon(category, scope);
CREATE TABLE IF NOT EXISTS buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_buffer_umo ON buffer(umo);
"""


class JargonStore:
    def __init__(self, db_path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            try:  # 老库补列：记住提升前的群号，取消全局时能还原
                self._conn.execute(
                    "ALTER TABLE jargon ADD COLUMN origin_scope TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jargon_updated ON jargon(updated_at)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_buffer_ts ON buffer(ts)")
            self._conn.execute(
                "DELETE FROM buffer WHERE ts < ?", (time.time() - 7 * 86400,))
            self._conn.commit()

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ---------- 消息缓冲（持久化，重启不丢） ----------

    def buffer_add(self, umo: str, sender: str, text: str) -> int:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO buffer(umo, sender, text, ts) VALUES(?,?,?,?)",
                (umo, sender, text[:500], now),
            )
            self._conn.commit()
            return self._conn.execute(
                "SELECT COUNT(*) FROM buffer WHERE umo = ?", (umo,)
            ).fetchone()[0]

    def buffer_take(self, umo: str, limit: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sender, text FROM buffer WHERE umo = ? ORDER BY id LIMIT ?",
                (umo, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def buffer_drop(self, umo: str, up_to_id: int = 0):
        with self._lock:
            if up_to_id:
                self._conn.execute("DELETE FROM buffer WHERE umo = ? AND id <= ?", (umo, up_to_id))
            else:
                self._conn.execute("DELETE FROM buffer WHERE umo = ?", (umo,))
            self._conn.commit()

    def buffer_clear(self, umo: str):
        self.buffer_drop(umo)

    # ---------- 词条读写 ----------

    def upsert(self, name: str, meaning: str, category: str, scope: str,
               aliases: str = "", evidence: str = "",
               auto_review: bool = False, auto_note: str = "") -> dict:
        name = (name or "").strip()[:32]
        meaning = (meaning or "").strip()[:1000]
        if not name or not meaning:
            return {}
        if category not in CATEGORIES:
            category = "group"
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jargon WHERE name = ? AND category = ? AND scope = ?",
                (name, category, scope),
            ).fetchone()
            if row:
                rec = dict(row)
                # 手动条目永不被覆盖
                if rec["manual_review"]:
                    return rec
                self._conn.execute(
                    """UPDATE jargon SET meaning = ?, aliases = ?, count = count + 1,
                       auto_review = MAX(auto_review, ?), auto_note = ?, evidence = ?,
                       updated_at = ? WHERE id = ?""",
                    (meaning, aliases, int(auto_review), auto_note,
                     (evidence or rec["evidence"])[:400], now, rec["id"]),
                )
                self._conn.commit()
                rec.update(meaning=meaning, count=rec["count"] + 1,
                           auto_review=max(rec["auto_review"], int(auto_review)),
                           auto_note=auto_note, updated_at=now)
                return rec
            cur = self._conn.execute(
                """INSERT INTO jargon(name, aliases, meaning, category, scope,
                   auto_review, auto_note, evidence, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (name, aliases, meaning, category, scope, int(auto_review),
                 auto_note, evidence[:400], now, now),
            )
            self._conn.commit()
            return dict(self._conn.execute(
                "SELECT * FROM jargon WHERE id = ?", (cur.lastrowid,)).fetchone())

    def get(self, key: str) -> dict | None:
        """按 id 或名字取一条（名字可能重名，取最新的）。"""
        with self._lock:
            if str(key).isdigit():
                row = self._conn.execute(
                    "SELECT * FROM jargon WHERE id = ?", (int(key),)).fetchone()
                if row:
                    return dict(row)
            row = self._conn.execute(
                "SELECT * FROM jargon WHERE name = ? ORDER BY updated_at DESC LIMIT 1",
                (str(key),),
            ).fetchone()
            return dict(row) if row else None

    def eligible(self, scope: str, include_pending: bool = False) -> list[dict]:
        """可注入的条目：全局 + 本群 + 无群归属的手动条目。"""
        cond = "(manual_review = 1 OR auto_review = 1)"
        if include_pending:
            cond = "1 = 1"
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM jargon
                    WHERE {cond}
                      AND (category = 'global' OR scope = '' OR scope = ?)""",
                (scope,),
            ).fetchall()
            return [dict(r) for r in rows]

    def query(self, kind: str = "all", scope: str = "", page: int = 1,
              size: int = 10) -> tuple[int, list[dict]]:
        where, params = [], []
        if kind == "pending":
            where.append("manual_review = 0 AND auto_review = 0")
        elif kind == "auto":
            where.append("auto_review = 1 AND manual_review = 0")
        elif kind == "manual":
            where.append("manual_review = 1")
        if scope:
            where.append("(category = 'group' AND scope = ?)")
            params.append(scope)
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM jargon {sql_where}", params).fetchone()[0]
            rows = self._conn.execute(
                f"""SELECT * FROM jargon {sql_where}
                    ORDER BY manual_review DESC, auto_review DESC, count DESC, id DESC
                    LIMIT ? OFFSET ?""",
                (*params, size, (page - 1) * size),
            ).fetchall()
            return total, [dict(r) for r in rows]

    def approve(self, key: str) -> dict | None:
        rec = self.get(key)
        if not rec:
            return None
        with self._lock:
            self._conn.execute(
                "UPDATE jargon SET manual_review = 1, auto_review = 1, updated_at = ? WHERE id = ?",
                (time.time(), rec["id"]),
            )
            self._conn.commit()
        return self.get(str(rec["id"]))

    def delete(self, key: str) -> bool:
        rec = self.get(key)
        if not rec:
            return False
        with self._lock:
            self._conn.execute("DELETE FROM jargon WHERE id = ?", (rec["id"],))
            self._conn.commit()
        return True

    def promote(self, key: str) -> bool:
        """把群条目提升为全局，记住原来的群号。"""
        rec = self.get(key)
        if not rec:
            return False
        origin = rec["scope"] if rec["scope"] else (rec.get("origin_scope") or "")
        with self._lock:
            try:
                self._conn.execute(
                    """UPDATE jargon SET category = 'global', scope = '',
                       origin_scope = ?, updated_at = ? WHERE id = ?""",
                    (origin, time.time(), rec["id"]),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def set_review(self, key: str, manual: bool | None = None,
                   auto: bool | None = None) -> dict | None:
        """单独设置审核标记，可上可下。"""
        rec = self.get(key)
        if not rec:
            return None
        sets, params = [], []
        if manual is not None:
            sets.append("manual_review = ?")
            params.append(int(manual))
        if auto is not None:
            sets.append("auto_review = ?")
            params.append(int(auto))
        if not sets:
            return rec
        params.extend([time.time(), rec["id"]])
        with self._lock:
            self._conn.execute(
                f"UPDATE jargon SET {', '.join(sets)}, updated_at = ? WHERE id = ?", params)
            self._conn.commit()
        return self.get(str(rec["id"]))

    def set_scope(self, key: str, scope: str) -> dict | None:
        """改归属：scope 为空表示全局。"""
        rec = self.get(key)
        if not rec:
            return None
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE jargon SET scope = ?, updated_at = ? WHERE id = ?",
                    (scope, time.time(), rec["id"]),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                return None
        return self.get(str(rec["id"]))

    def update_name(self, key: str, new_name: str) -> dict | None:
        """改词条名；重名会被数据库约束挡住。"""
        rec = self.get(key)
        new_name = (new_name or "").strip()[:32]
        if not rec or not new_name or new_name == rec["name"]:
            return rec
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE jargon SET name = ?, updated_at = ? WHERE id = ?",
                    (new_name, time.time(), rec["id"]),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                return None
        return self.get(str(rec["id"]))

    def update_meaning(self, key: str, meaning: str) -> dict | None:
        """只改含义，不动归属和审核标记。"""
        rec = self.get(key)
        meaning = (meaning or "").strip()[:1000]
        if not rec or not meaning:
            return None
        with self._lock:
            self._conn.execute(
                "UPDATE jargon SET meaning = ?, updated_at = ? WHERE id = ?",
                (meaning, time.time(), rec["id"]),
            )
            self._conn.commit()
        return self.get(str(rec["id"]))

    def demote(self, key: str) -> bool:
        """取消全局：回到原来的群；没有原群就挂起。"""
        rec = self.get(key)
        if not rec:
            return False
        scope = rec.get("origin_scope") or "__off__"
        with self._lock:
            self._conn.execute(
                """UPDATE jargon SET category = 'group', scope = ?, updated_at = ?
                   WHERE id = ?""",
                (scope, time.time(), rec["id"]),
            )
            self._conn.commit()
        return True

    def add_manual(self, name: str, meaning: str, scope: str = "",
                   aliases: str = "") -> dict:
        rec = self.upsert(name, meaning, "manual", scope, aliases=aliases,
                          evidence="手动录入")
        with self._lock:
            self._conn.execute(
                "UPDATE jargon SET manual_review = 1, auto_review = 1 WHERE id = ?",
                (rec["id"],),
            )
            self._conn.commit()
        return self.get(str(rec["id"])) or rec

    def stats(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT category, COUNT(*) n FROM jargon GROUP BY category").fetchall()
            by_cat = {r["category"]: r["n"] for r in rows}
            total = sum(by_cat.values())
            pending = self._conn.execute(
                "SELECT COUNT(*) FROM jargon WHERE manual_review = 0 AND auto_review = 0"
            ).fetchone()[0]
            auto_ok = self._conn.execute(
                "SELECT COUNT(*) FROM jargon WHERE auto_review = 1 AND manual_review = 0"
            ).fetchone()[0]
            manual = self._conn.execute(
                "SELECT COUNT(*) FROM jargon WHERE manual_review = 1").fetchone()[0]
        return {"total": total, "pending": pending, "auto_ok": auto_ok,
                "manual": manual, "by_category": by_cat}

    @staticmethod
    def dumps(rec: dict) -> str:
        return json.dumps(rec, ensure_ascii=False)
