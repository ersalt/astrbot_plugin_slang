# -*- coding: utf-8 -*-
"""SQLite 存储层 v2。

结构变更（相对 v1）：
- 去掉 category，用 scope（'' = 全局 / 群号）+ source（manual / ai）表达
- 唯一键改为 (name, scope)：同一作用域里一个词只有一条
- evidence 存 JSON 数组，最多 3 条
- 启动时自动把 v1 迁移到 v2，并留一份 .v1.bak
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
    origin_scope TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 1,
    review_auto INTEGER NOT NULL DEFAULT 0,
    review_manual INTEGER NOT NULL DEFAULT 0,
    auto_note TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(name, scope)
);
CREATE INDEX IF NOT EXISTS idx_jargon_scope ON jargon(scope);
CREATE INDEX IF NOT EXISTS idx_jargon_updated ON jargon(updated_at);
CREATE INDEX IF NOT EXISTS idx_jargon_name ON jargon(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
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


class JargonStore:
    def __init__(self, db_path: str):
        self._lock = threading.RLock()
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_from_v1()
            self._conn.execute("DELETE FROM buffer WHERE ts < ?", (time.time() - 7 * 86400,))
            self._conn.commit()

    # ---------- 迁移 ----------

    def _migrate_from_v1(self):
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(jargon)")}
        if "category" not in cols:
            return
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
                r["_scope"], r["_source"] = scope, (
                    "manual" if r.get("category") == "manual" else "ai")
                merged[key] = r
                continue
            cur["count"] += r.get("count", 1)
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
                   origin_scope, count, review_auto, review_manual, auto_note,
                   evidence, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r.get("id"), name, r.get("aliases") or "", r.get("meaning") or "",
                 scope, r["_source"], r.get("origin_scope") or "", r.get("count") or 1,
                 r.get("auto_review") or 0, r.get("manual_review") or 0,
                 r.get("auto_note") or "", self._pack_evidence(r.get("evidence")),
                 r.get("created_at") or now, r.get("updated_at") or now),
            )
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version', '2')")
        self._conn.commit()

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

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ---------- 消息缓冲 ----------

    def buffer_add(self, umo: str, sender: str, text: str) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO buffer(umo, sender, text, ts) VALUES(?,?,?,?)",
                (umo, sender, text[:500], time.time()),
            )
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

    # ---------- 词条 ----------

    def upsert(self, name: str, meaning: str, scope: str = "",
               aliases: str = "", evidence: str = "", source: str = "ai",
               auto_review: bool = False, auto_note: str = "") -> dict:
        name = (name or "").strip()[:32]
        meaning = (meaning or "").strip()[:1000]
        if not name or not meaning:
            return {}
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jargon WHERE name = ? AND scope = ?",
                (name, scope)).fetchone()
            if row:
                rec = dict(row)
                if rec["review_manual"]:
                    return rec
                ev = self._merge_evidence(rec["evidence"], evidence)
                self._conn.execute(
                    """UPDATE jargon SET meaning = ?, aliases = ?, count = count + 1,
                       review_auto = MAX(review_auto, ?), auto_note = ?, evidence = ?,
                       updated_at = ? WHERE id = ?""",
                    (meaning, aliases, int(auto_review), auto_note, ev, now, rec["id"]))
                self._conn.commit()
                return self.get(str(rec["id"])) or rec
            self._conn.execute(
                """INSERT INTO jargon(name, aliases, meaning, scope, source,
                   review_auto, auto_note, evidence, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (name, aliases, meaning, scope, source, int(auto_review),
                 auto_note, self._pack_evidence(evidence), now, now))
            self._conn.commit()
            return self.get(name) or {}

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

    def get(self, key: str) -> dict | None:
        with self._lock:
            if str(key).isdigit():
                row = self._conn.execute(
                    "SELECT * FROM jargon WHERE id = ?", (int(key),)).fetchone()
                if row:
                    return dict(row)
            row = self._conn.execute(
                "SELECT * FROM jargon WHERE name = ? ORDER BY updated_at DESC LIMIT 1",
                (str(key),)).fetchone()
            return dict(row) if row else None

    def eligible(self, scope: str, include_pending: bool = False) -> list[dict]:
        cond = "1 = 1" if include_pending else "(review_manual = 1 OR review_auto = 1)"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM jargon WHERE {cond} AND (scope = '' OR scope = ?)",
                (scope,)).fetchall()
            return [dict(r) for r in rows]

    def query(self, kind: str = "all", page: int = 1,
              size: int = 10) -> tuple[int, list[dict]]:
        where = []
        if kind == "pending":
            where.append("review_manual = 0 AND review_auto = 0")
        elif kind == "auto":
            where.append("review_auto = 1 AND review_manual = 0")
        elif kind == "manual":
            where.append("review_manual = 1")
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM jargon {sql_where}").fetchone()[0]
            rows = self._conn.execute(
                f"""SELECT * FROM jargon {sql_where}
                    ORDER BY review_manual DESC, review_auto DESC, count DESC, id DESC
                    LIMIT ? OFFSET ?""",
                (size, (page - 1) * size)).fetchall()
            return total, [dict(r) for r in rows]

    def set_review(self, key: str, manual: bool | None = None,
                   auto: bool | None = None) -> dict | None:
        rec = self.get(key)
        if not rec:
            return None
        sets, params = [], []
        if manual is not None:
            sets.append("review_manual = ?")
            params.append(int(manual))
        if auto is not None:
            sets.append("review_auto = ?")
            params.append(int(auto))
        if not sets:
            return rec
        params.extend([time.time(), rec["id"]])
        with self._lock:
            self._conn.execute(
                f"UPDATE jargon SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                params)
            self._conn.commit()
        return self.get(str(rec["id"]))

    def approve(self, key: str) -> dict | None:
        return self.set_review(key, manual=True, auto=True)

    def update_name(self, key: str, new_name: str) -> dict | None:
        rec = self.get(key)
        new_name = (new_name or "").strip()[:32]
        if not rec or not new_name or new_name == rec["name"]:
            return rec
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE jargon SET name = ?, updated_at = ? WHERE id = ?",
                    (new_name, time.time(), rec["id"]))
                self._conn.commit()
            except sqlite3.IntegrityError:
                return None
        return self.get(str(rec["id"]))

    def update_meaning(self, key: str, meaning: str) -> dict | None:
        rec = self.get(key)
        meaning = (meaning or "").strip()[:1000]
        if not rec or not meaning:
            return None
        with self._lock:
            self._conn.execute(
                "UPDATE jargon SET meaning = ?, updated_at = ? WHERE id = ?",
                (meaning, time.time(), rec["id"]))
            self._conn.commit()
        return self.get(str(rec["id"]))

    def set_scope(self, key: str, scope: str) -> dict | None:
        rec = self.get(key)
        if not rec:
            return None
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE jargon SET scope = ?, updated_at = ? WHERE id = ?",
                    (scope, time.time(), rec["id"]))
                self._conn.commit()
            except sqlite3.IntegrityError:
                return None
        return self.get(str(rec["id"]))

    def delete(self, key: str) -> bool:
        rec = self.get(key)
        if not rec:
            return False
        with self._lock:
            self._conn.execute("DELETE FROM jargon WHERE id = ?", (rec["id"],))
            self._conn.commit()
        return True

    def _set_scope_safe(self, rec: dict, scope: str) -> bool:
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE jargon SET scope = ?, updated_at = ? WHERE id = ?",
                    (scope, time.time(), rec["id"]))
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def promote(self, key: str) -> bool:
        rec = self.get(key)
        if not rec:
            return False
        origin = rec["scope"] or rec.get("origin_scope") or ""
        with self._lock:
            try:
                self._conn.execute(
                    """UPDATE jargon SET scope = '', origin_scope = ?,
                       updated_at = ? WHERE id = ?""",
                    (origin, time.time(), rec["id"]))
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def demote(self, key: str) -> bool:
        rec = self.get(key)
        if not rec:
            return False
        return self._set_scope_safe(rec, rec.get("origin_scope") or "__off__")

    def add_manual(self, name: str, meaning: str, scope: str = "",
                   aliases: str = "") -> dict:
        rec = self.upsert(name, meaning, scope=scope, aliases=aliases,
                          source="manual", evidence="手动录入")
        if not rec:
            return {}
        with self._lock:
            self._conn.execute(
                "UPDATE jargon SET review_manual = 1, review_auto = 1, source = 'manual' "
                "WHERE id = ?", (rec["id"],))
            self._conn.commit()
        return self.get(str(rec["id"])) or rec

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM jargon").fetchone()[0]
            pending = self._conn.execute(
                "SELECT COUNT(*) FROM jargon WHERE review_manual = 0 AND review_auto = 0").fetchone()[0]
            auto_ok = self._conn.execute(
                "SELECT COUNT(*) FROM jargon WHERE review_auto = 1 AND review_manual = 0").fetchone()[0]
            manual = self._conn.execute(
                "SELECT COUNT(*) FROM jargon WHERE review_manual = 1").fetchone()[0]
            global_n = self._conn.execute(
                "SELECT COUNT(*) FROM jargon WHERE scope = ''").fetchone()[0]
            group_n = total - global_n
        return {"total": total, "pending": pending, "auto_ok": auto_ok,
                "manual": manual,
                "by_category": {"global": global_n, "group": group_n, "manual": manual}}
